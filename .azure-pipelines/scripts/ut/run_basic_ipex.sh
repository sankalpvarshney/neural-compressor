#!/bin/bash
python -c "import neural_compressor as nc;print(nc.version.__version__)"
echo "run basic ipex"

echo "specify fwk version..."
source /neural-compressor/.azure-pipelines/scripts/ut/ut_fwk_version.sh $1

echo "set up UT env..."
bash /neural-compressor/.azure-pipelines/scripts/ut/env_setup.sh
export COVERAGE_RCFILE=/neural-compressor/.azure-pipelines/scripts/ut/coverage.file
lpot_path=$(python -c 'import neural_compressor; import os; print(os.path.dirname(neural_compressor.__file__))')
cd /neural-compressor/test || exit 1
find ./ipex -name "test*.py" | sed 's,\.\/,coverage run --source='"${lpot_path}"' --append ,g' | sed 's/$/ --verbose/'> run.sh

LOG_DIR=/neural-compressor/log_dir
mkdir -p ${LOG_DIR}
ut_log_name=${LOG_DIR}/ut_ipex.log

echo "cat run.sh..."
cat run.sh | tee ${ut_log_name}
echo "------UT start-------"
bash run.sh 2>&1 | tee -a ${ut_log_name}
cp .coverage ${LOG_DIR}/.coverage.ipex
echo "------UT end -------"

if [ $(grep -c "FAILED" ${ut_log_name}) != 0 ] || [ $(grep -c "OK" ${ut_log_name}) == 0 ];then
    echo "Find errors in UT test, please check the output..."
    exit 1
fi
echo "UT finished successfully! "
