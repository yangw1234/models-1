RANDOM_SEED=`date +%s`
QUALITY=0.759
set -e

# Register the model as a source root
export PYTHONPATH="$(pwd):${PYTHONPATH}"

# MLPerf
export PYTHONPATH="$(pwd)/../:$(pwd)/../../../../../common/tensorflow/:${PYTHONPATH}"
echo $PYTHONPATH

# export OMP_NUM_THREADS=4
export KMP_BLOCKTIME=0

# 4 instances
export OMP_NUM_THREADS=6
export KMP_AFFINITY=disable
export KMP_SETTINGS=1

# numactl --cpunodebind=3 --membind=3 --physcpubind=73-94,169-93 python imagenet_main.py 1 --model_dir ./logs --batch_size 512 --version 1 --resnet_size 50 --data_dir /mnt/dataset/ILSVRC2012 --use_bfloat16 --use_synthetic_data
# export SPARK_DRIVER_MEMORY=100g
# export MASTER=local[16]

export ANALYTICS_ZOO_HOME=/home/cpx/yang/dist/
export SPARK_HOME=/opt/spark-2.4.3-bin-hadoop2.7
# python train_spines2.py
bash $ANALYTICS_ZOO_HOME/bin/spark-submit-python-with-zoo.sh --master spark://cpx-1:7077 --executor-cores 1 --total-executor-cores 16 --driver-memory 20g --executor-memory 45g  imagenet_main.py 1 --model_dir ./logs --batch_size 128 --version 1 --resnet_size 50 --use_bfloat16 --use_synthetic_data

# mpirun --allow-run-as-root -n 1 --map-by ppr:1:socket:pe=24 --bind-to core:overload-allowed python imagenet_main.py $RANDOM_SEED --data_dir /mnt/dataset/ILSVRC2012 \
#         --model_dir $MODEL_DIR --train_epochs 10000 --stop_threshold $QUALITY --batch_size 128 \
#           --version 1 --resnet_size 50 --epochs_between_evals 4 --weight_decay 1e-4 --inter_op_parallelism_threads 2 --intra_op_parallelism_threads 4 --use_bfloat16

