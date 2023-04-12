// Copyright (c) 2021 Intel Corporation
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { Subject } from 'rxjs';
import { environment } from 'src/environments/environment';
import { ErrorComponent } from '../error/error.component';
import { WarningComponent } from '../warning/warning.component';

@Injectable({
  providedIn: 'root'
})
export class ModelService {

  baseUrl = environment.baseUrl;
  workspacePath: string;
  projectCount = 0;

  colorMode$: Subject<string> = new Subject<''>();
  projectCreated$: Subject<boolean> = new Subject<boolean>();
  datasetCreated$: Subject<boolean> = new Subject<boolean>();
  openDatasetDialog$: Subject<boolean> = new Subject<boolean>();
  optimizationCreated$: Subject<boolean> = new Subject<boolean>();
  benchmarkCreated$: Subject<boolean> = new Subject<boolean>();
  projectChanged$: Subject<any> = new Subject<any>();
  getNodeDetails$: Subject<string> = new Subject<string>();

  token;
  systemInfo: {
    systeminfo: {
      bf16_support: boolean;
      cores_per_socket: number;
      sockets: number;
      system: string;
    };
    frameworks: object;
  };

  constructor(
    private http: HttpClient,
    public dialog: MatDialog
  ) { }

  getToken(): string {
    return this.token;
  }

  setToken(token: string) {
    this.token = token;
  }

  getSystemInfo() {
    this.http.get(
      this.baseUrl + 'api/system_info'
    ).subscribe(
      (resp: {
        systeminfo: {
          bf16_support: boolean;
          cores_per_socket: number;
          sockets: number;
          system: string;
        };
        frameworks: object;
      }) => {
        this.systemInfo = resp;
      },
      error => {
        this.openErrorDialog(error);
      }
    );
  }

  getJobsQueue() {
    return this.http.get(
      this.baseUrl + 'api/jobs/queue'
    );
  }

  getDefaultPath(name: string) {
    return this.http.post(
      this.baseUrl + 'api/get_default_path',
      { name }
    );
  }

  getModelGraph(path: string, groups?: string[]) {
    let groupsParam = '';
    if (groups) {
      groups.forEach(group => {
        groupsParam += '&group=' + group;
      });
    }
    return this.http.get(
      this.baseUrl + 'api/model/graph' + '?path=' + path + groupsParam
    );
  }

  highlightPatternInGraph(path: string, op_name: string, pattern: string[]) {
    return this.http.post(
      this.baseUrl + 'api/model/graph/highlight_pattern',
      {
        path: [path],
        op_name,
        pattern
      }
    );
  }

  getDictionary(param: string) {
    return this.http.get(this.baseUrl + 'api/dict/' + param);
  }

  getDictionaryWithParam(path: string, paramName: string, param: any) {
    return this.http.post(this.baseUrl + 'api/dict/' + path + '/' + paramName, param);
  }

  getPossibleValues(param: string, config?: any) {
    return this.http.post(
      this.baseUrl + 'api/get_possible_values',
      {
        param,
        config
      }
    );
  }

  getFileSystem(path: string, filter: FileBrowserFilter) {
    if (filter === 'all') {
      return this.http.get(this.baseUrl + 'api/filesystem', {
        params: {
          path: path + '/'
        }
      });
    }
    return this.http.get(this.baseUrl + 'api/filesystem', {
      params: {
        path: path + '/',
        filter
      }
    });
  }

  getExamplesList() {
    return this.http.get(this.baseUrl + 'api/examples/list');
  }

  addExample(newProject) {
    return this.http.post(this.baseUrl + 'api/examples/add', newProject);
  }

  getProjectList() {
    return this.http.get(this.baseUrl + 'api/project/list');
  }

  getProjectDetails(id: number) {
    return this.http.post(this.baseUrl + 'api/project', { id });
  }

  createProject(newProject) {
    return this.http.post(this.baseUrl + 'api/project/create',
      newProject
    );
  }

  removeProject(projectId, projectName) {
    return this.http.post(this.baseUrl + 'api/project/delete',
      {
        id: projectId,
        name: projectName
      }
    );
  }

  getDatasetList(id: number) {
    return this.http.post(this.baseUrl + 'api/dataset/list', { project_id: id });
  }

  getDatasetDetails(id: number) {
    return this.http.post(this.baseUrl + 'api/dataset', { id });
  }

  getPredefinedDatasets(framework: FrameworkName, domain: DomainName, domainFlavour: DomainFlavourName) {
    return this.http.post(this.baseUrl + 'api/dataset/predefined', {
      framework,
      domain,
      domain_flavour: domainFlavour
    });
  }

  addDataset(dataset) {
    return this.http.post(this.baseUrl + 'api/dataset/add', dataset);
  }

  getOptimizationList(id: number) {
    return this.http.post(this.baseUrl + 'api/optimization/list', { project_id: id });
  }

  getOptimizationDetails(id: number) {
    return this.http.post(this.baseUrl + 'api/optimization', { id });
  }

  addOptimization(optimization) {
    return this.http.post(this.baseUrl + 'api/optimization/add', optimization);
  }

  executeOptimization(optimizationId: number, requestId: string) {
    return this.http.post(
      this.baseUrl + 'api/optimization/execute',
      {
        request_id: requestId,
        optimization_id: optimizationId,
      }
    );
  }

  editOptimization(details: any) {
    return this.http.post(
      this.baseUrl + 'api/optimization/edit',
      details
    );
  }

  pinBenchmark(optimizationId: number, benchmarkId: number, mode: string) {
    if (mode === 'accuracy') {
      return this.http.post(this.baseUrl + 'api/optimization/pin_accuracy_benchmark',
        {
          optimization_id: optimizationId,
          benchmark_id: benchmarkId
        });
    } else if (mode === 'performance') {
      return this.http.post(this.baseUrl + 'api/optimization/pin_performance_benchmark',
        {
          optimization_id: optimizationId,
          benchmark_id: benchmarkId
        });
    }
  }

  addNotes(id: number, notes: string) {
    return this.http.post(this.baseUrl + 'api/project/note', {
      id,
      notes
    });
  }

  getModelList(id: number) {
    return this.http.post(this.baseUrl + 'api/model/list', { project_id: id });
  }

  getBenchmarksList(id: number) {
    return this.http.post(this.baseUrl + 'api/benchmark/list', { project_id: id });
  }

  addBenchmark(benchmark) {
    return this.http.post(this.baseUrl + 'api/benchmark/add', benchmark);
  }

  getBenchmarkDetails(id: number) {
    return this.http.post(this.baseUrl + 'api/benchmark', { id });
  }

  executeBenchmark(benchmarkId: number, requestId: number) {
    return this.http.post(
      this.baseUrl + 'api/benchmark/execute',
      {
        request_id: requestId,
        benchmark_id: benchmarkId,
      }
    );
  }

  editBenchmark(details: any) {
    return this.http.post(
      this.baseUrl + 'api/benchmark/edit',
      details
    );
  }

  addProfiling(profiling) {
    return this.http.post(this.baseUrl + 'api/profiling/add', profiling);
  }

  getProfilingList(id: number) {
    return this.http.post(this.baseUrl + 'api/profiling/list', { project_id: id });
  }

  getProfilingDetails(id: number) {
    return this.http.post(this.baseUrl + 'api/profiling', { id });
  }

  executeProfiling(profilingId, requestId) {
    return this.http.post(
      this.baseUrl + 'api/profiling/execute',
      {
        profiling_id: profilingId,
        request_id: requestId,
      }
    );
  }

  editProfiling(details: any) {
    return this.http.post(
      this.baseUrl + 'api/profiling/edit',
      details
    );
  }

  downloadModel(modelId: number) {
    return this.http.post(
      this.baseUrl + 'api/model/download',
      { id: modelId },
      { responseType: 'blob' }
    );
  }

  downloadProfiling(profilingId: number) {
    return this.http.post(
      this.baseUrl + 'api/profiling/results/csv',
      { id: profilingId.toString() },
      { responseType: 'blob' }
    );
  }

  delete(what: string, id: number, name: string) {
    return this.http.post(
      this.baseUrl + `api/${what}/delete`,
      {
        id,
        name
      }
    );
  }

  getOpList(project_id: number, model_id: number) {
    return this.http.post(
      this.baseUrl + `api/diagnosis/op_list`,
      {
        project_id,
        model_id
      }
    );
  }

  getOpDetails(project_id: number, model_id: number, op_name: string) {
    return this.http.post(
      this.baseUrl + `api/diagnosis/op_details`,
      {
        project_id,
        model_id,
        op_name
      }
    );
  }

  generateOptimization(changes: any) {
    return this.http.post(
      this.baseUrl + `api/diagnosis/generate_optimization`,
      changes
    );
  }

  getModelWiseParams(optimization_id: number) {
    return this.http.post(
      this.baseUrl + `api/diagnosis/model_wise_params`,
      {
        optimization_id
      }
    );
  }

  getHistogram(project_id: number, model_id: number, op_name: string, type: 'activation' | 'weights') {
    return this.http.post(
      this.baseUrl + `api/diagnosis/histogram`,
      {
        project_id,
        model_id,
        op_name,
        type

      }
    );
  }

  getPruningDetails(optimization_id: number) {
    return this.http.post(
      this.baseUrl + `api/optimization/pruning_details`,
      {
        id: optimization_id
      }
    );
  }

  getPruningParams(optimization_id: number) {
    return this.http.post(
      this.baseUrl + `api/pruning/details_wizard`,
      {
        optimization_id
      }
    );
  }

  openErrorDialog(error) {
    const dialogRef = this.dialog.open(ErrorComponent, {
      data: error
    });
  }

  openWarningDialog(warning) {
    const dialogRef = this.dialog.open(WarningComponent, {
      data: warning
    });
  }
}

export interface NewModel {
  domain: string;
  domain_flavour: string;
  framework: string;
  id: string;
  input?: string;
  model_path: string;
  output?: string;
}

export type FileBrowserFilter = 'models' | 'datasets' | 'directories' | 'all';
export type DomainName = 'Image Recognition' | 'Object Detection' | 'Neural Language Processing' | 'Recommendation';
export type DomainFlavourName = 'SSD' | 'Yolo' | '' | null;
export type FrameworkName = 'TensorFlow' | 'ONNXRT' | 'PyTorch';
