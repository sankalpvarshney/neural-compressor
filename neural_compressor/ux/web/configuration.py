# -*- coding: utf-8 -*-
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Configuration module for UX server."""

import argparse
import logging
import os
import secrets
import socket
import sys
from typing import Dict

from numpy.random import randint

from neural_compressor.utils.utility import singleton
from neural_compressor.ux.utils.consts import WORKDIR_LOCATION, WORKSPACE_LOCATION
from neural_compressor.ux.utils.exceptions import NotFoundException
from neural_compressor.ux.utils.logger import log
from neural_compressor.ux.utils.utils import determine_ip


@singleton
class Configuration:
    """Configuration object for UX server."""

    PORT_DEFAULT = 5000
    MAX_PORTS_TRIED = 10

    def __init__(self) -> None:
        """Set the variables."""
        self.server_address = ""
        self.server_port = 0
        self.url_prefix: str = ""
        self.gui_port = 0
        self.log_level = 0
        self.token = ""
        self.scheme = ""
        self.workdir = ""
        self.allow_insecure_connections = False
        self.tls_certificate = ""
        self.tls_key = ""
        self.set_up()

    def set_up(self) -> None:
        """Reset variables."""
        self.determine_values_from_environment()

    def determine_values_from_environment(self) -> None:
        """Set variables based on environment values."""
        args = self.get_command_line_args()
        self.server_address = determine_ip()
        self.server_port = self.determine_server_port(args)
        self.url_prefix = self.determine_url_prefix(args)
        self.gui_port = self.determine_gui_port(args)
        self.log_level = self.determine_log_level(args)
        self.token = secrets.token_hex(16)
        self.allow_insecure_connections = args.get("allow_insecure_connections", False)
        self.tls_certificate = args.get("cert", "")
        self.tls_key = args.get("key", "")
        self.scheme = "http" if self.allow_insecure_connections else "https"
        self.workdir = WORKSPACE_LOCATION

    @property
    def global_config_directory(self) -> str:
        """Get the directory for global config files."""
        return os.path.join(
            os.environ.get("HOME", ""),
            ".neural_compressor",
        )

    def get_command_line_args(self) -> Dict:
        """Return arguments passed in command line."""
        parser = argparse.ArgumentParser(
            description="Run Intel(r) Neural Compressor Bench server.",
        )
        parser.add_argument(
            "-p",
            "--port",
            type=int,
            help="server port number to listen on",
        )
        parser.add_argument(
            "-P",
            "--gui-port",
            type=int,
            help="port number for GUI",
        )
        parser.add_argument(
            "-U",
            "--url-prefix",
            type=str,
            default="",
            help="URL prefix for INC Bench instance.",
        )
        parser.add_argument(
            "--allow-insecure-connections",
            action="store_true",
            help="run server without encryption",
        )
        parser.add_argument(
            "--cert",
            type=str,
            default="",
            help="TLS Certificate to use",
        )
        parser.add_argument(
            "--key",
            type=str,
            default="",
            help="TLS private key to use",
        )
        parser.add_argument(
            "--verbose",
            "-v",
            action="count",
            default=2,
            help="verbosity of logging output, use -vv and -vvv for even more logs",
        )
        return vars(parser.parse_args())

    def determine_server_port(self, args: Dict) -> int:
        """
        Return port to be used by the server.

        Will raise a NotFoundException if port is already in use.

        When port given in command line, only that port will be tried.
        When no port specified will try self.MAX_PORTS_TRIED times,
        starting with self.PORT_DEFAULT.
        """
        command_line_port = args.get("port")
        if command_line_port is not None:
            self._ensure_valid_port(command_line_port)
            if self.is_port_taken(command_line_port):
                raise NotFoundException(
                    f"Port {command_line_port} already in use, exiting.",
                )
            else:
                return command_line_port

        ports = [self.PORT_DEFAULT] + randint(
            1025,
            65536,
            self.MAX_PORTS_TRIED - 1,
        ).tolist()

        for port in ports:
            if not self.is_port_taken(port):
                return port

        raise NotFoundException(
            f"Unable to find a free port in {len(ports)} attempts, exiting.",
        )

    def determine_gui_port(self, args: Dict) -> int:
        """
        Return port to be used by the GUI client.

        Will return self.server_port unless specified in configuration.
        """
        command_line_port = args.get("gui_port")
        if command_line_port is not None:
            self._ensure_valid_port(command_line_port)
            return command_line_port
        return self.server_port

    def is_port_taken(self, port: int) -> bool:
        """Return if given port is already in use."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            s.bind((self.server_address, port))
        except socket.error:
            return True
        finally:
            s.close()

        return False

    def determine_log_level(self, args: Dict) -> int:
        """Determine log level based on parameters given."""
        verbosity_mapping = [
            logging.CRITICAL,
            logging.WARNING,
            logging.INFO,
            logging.DEBUG,
        ]
        verbosity: int = args.get("verbose")  # type:ignore
        try:
            return verbosity_mapping[verbosity]
        except IndexError:
            return logging.DEBUG

    @staticmethod
    def determine_url_prefix(args: dict) -> str:
        """Determine url prefix based on parameters given."""
        url_prefix = args.get("url_prefix", "")
        if isinstance(url_prefix, str) and not url_prefix.startswith("/"):
            url_prefix = f"/{url_prefix}"
        return url_prefix

    def get_url(self) -> str:
        """Return URL to access application."""
        base_url = f"{self.scheme}://{self.server_address}:{self.gui_port}"
        if self.url_prefix != "/":
            base_url = f"{base_url}{self.url_prefix}"
        return f"{base_url}/?token={self.token}"

    def dump_token_to_file(self) -> None:
        """Dump token to file."""
        token_filepath = os.path.join(WORKDIR_LOCATION, "token")
        with open(token_filepath, "w") as token_file:
            token_file.write(self.token)

        if sys.platform == "win32":
            import ntsecuritycon as con  # pylint: disable=import-error
            import win32api  # pylint: disable=import-error
            import win32security  # pylint: disable=import-error

            user, _, _ = win32security.LookupAccountName("", win32api.GetUserName())
            security_descriptor = win32security.GetFileSecurity(
                token_filepath,
                win32security.DACL_SECURITY_INFORMATION,
            )
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAce(
                win32security.ACL_REVISION,
                con.FILE_GENERIC_READ | con.FILE_GENERIC_WRITE,
                user,
            )
            security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
            win32security.SetFileSecurity(
                token_filepath,
                win32security.DACL_SECURITY_INFORMATION,
                security_descriptor,
            )
        else:
            os.chown(token_filepath, uid=os.geteuid(), gid=os.getgid())
            os.chmod(token_filepath, 0o600)
        log.debug(f"Token has been dumped to {token_filepath}.")

    def _ensure_valid_port(self, port: int) -> None:
        """Validate if proposed port number is allowed by TCP/IP."""
        if port < 1:
            raise ValueError(f"Lowest allowed port number is 1, attempted to use: {port}")
        if port > 65535:
            raise ValueError(f"Highest allowed port number is 65535, attempted to use: {port}")
