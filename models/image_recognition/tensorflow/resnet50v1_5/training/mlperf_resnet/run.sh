# Register the model as a source root
export PYTHONPATH="$(pwd):${PYTHONPATH}"
export KMP_BLOCKTIME=0

# 8 instances
export OMP_NUM_THREADS=6
export KMP_AFFINITY=granularity=fine,compact,1,0
export KMP_SETTINGS=1
export ANALYTICS_ZOO_HOME=/opt/work/analytics-zoo/dist/
export SPARK_HOME=/opt/work/spark-2.4.3
bash $ANALYTICS_ZOO_HOME/bin/spark-submit-python-with-zoo.sh --master spark://$(hostname):7077 --executor-cores 1 --total-executor-cores 8 --driver-memory 20g --executor-memory 18g --conf spark.network.timeout=10000000 --conf spark.executor.heartbeatInterval=100000 imagenet_main.py 1 --model_dir ./logs --batch_size 128 --version 1 --resnet_size 50 --train_epochs 90 --data_dir /opt/ILSVRC2012/ --use_bfloat16
