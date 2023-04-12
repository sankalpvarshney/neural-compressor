#!/bin/bash
python -c "import neural_compressor as nc;print(nc.version.__version__)"
echo "run coder"

echo "no FWKs need to be installed..."
echo "no requirements need to be installed..."

cd /neural-compressor/test || exit 1
find ./neural_coder -name "test*.py" | sed 's,\.\/,python ,g' | sed 's/$/ --verbose/' > run.sh

LOG_DIR=/neural-compressor/log_dir
mkdir -p ${LOG_DIR}
ut_log_name=${LOG_DIR}/ut_neural_coder.log

echo "cat run.sh..."
cat run.sh | tee ${ut_log_name}
echo "------UT start-------"
bash run.sh 2>&1 | tee -a ${ut_log_name}
echo "------UT end -------"

if [ $(grep -c "FAILED" ${ut_log_name}) != 0 ] || [ $(grep -c "OK" ${ut_log_name}) == 0 ];then
    echo "Find errors in UT test, please check the output..."
    exit 1
fi
echo "UT finished successfully! "