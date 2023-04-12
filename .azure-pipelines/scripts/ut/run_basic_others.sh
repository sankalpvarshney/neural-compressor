#!/bin/bash
python -c "import neural_compressor as nc;print(nc.version.__version__)"
echo "run basic others"

echo "specify fwk version..."
source /neural-compressor/.azure-pipelines/scripts/ut/ut_fwk_version.sh $1

echo "set up UT env..."
bash /neural-compressor/.azure-pipelines/scripts/ut/env_setup.sh
export COVERAGE_RCFILE=/neural-compressor/.azure-pipelines/scripts/ut/coverage.file
lpot_path=$(python -c 'import neural_compressor; import os; print(os.path.dirname(neural_compressor.__file__))')
cd /neural-compressor/test || exit 1
find . -name "test*.py" | sed 's,\.\/,coverage run --source='"${lpot_path}"' --append ,g' | sed 's/$/ --verbose/'> run.sh
sed -i '/ adaptor\//d' run.sh
sed -i '/ tfnewapi\//d' run.sh
sed -i '/ ux\//d' run.sh
sed -i '/ neural_coder\//d' run.sh
sed -i '/ ipex\//d' run.sh
sed -i '/ itex\//d' run.sh
sed -i '/ pruning/d' run.sh
sed -i '/ scheduler\//d' run.sh
sed -i '/ nas\//d' run.sh

echo "copy model for dynas..."
mkdir -p .torch/ofa_nets || true
cp -r /tf_dataset/ut-localfile/ofa_mbv3_d234_e346_k357_w1.2 .torch/ofa_nets || true

LOG_DIR=/neural-compressor/log_dir
mkdir -p ${LOG_DIR}
ut_log_name=${LOG_DIR}/ut_tf_${tensorflow_version}_pt_${pytorch_version}.log

echo "cat run.sh..."
cat run.sh | tee ${ut_log_name}
echo "------UT start-------"
bash run.sh 2>&1 | tee -a ${ut_log_name}
cp .coverage ${LOG_DIR}/.coverage.others
echo "------UT end -------"

if [ $(grep -c "FAILED" ${ut_log_name}) != 0 ] || [ $(grep -c "OK" ${ut_log_name}) == 0 ];then
    echo "Find errors in UT test, please check the output..."
    exit 1
fi
echo "UT finished successfully! "