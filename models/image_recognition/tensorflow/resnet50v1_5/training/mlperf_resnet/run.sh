RANDOM_SEED=`date +%s`
QUALITY=0.759
set -e

# Register the model as a source root
export PYTHONPATH="$(pwd):${PYTHONPATH}"

export KMP_BLOCKTIME=0

# 4 instances
export OMP_NUM_THREADS=6
export KMP_AFFINITY=disable
export KMP_SETTINGS=1

export ANALYTICS_ZOO_HOME=/home/cpx/yang/dist/
export SPARK_HOME=/opt/spark-2.4.3-bin-hadoop2.7
bash $ANALYTICS_ZOO_HOME/bin/spark-submit-python-with-zoo.sh --master spark://cpx-1:7077 --executor-cores 1 --total-executor-cores 16 --driver-memory 20g --executor-memory 45g  imagenet_main.py 1 --model_dir ./logs --batch_size 128 --version 1 --resnet_size 50 --use_bfloat16 --use_synthetic_data

