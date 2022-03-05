import abc
import collections
import configparser
import os
import socket
import threading
import time

import pyarrow
import tensorflow as tf
from pyspark import SparkContext, SparkConf

from helpers import create_s3_client, CounterAccumulatorParam, unpack_dict, pack_dict


def driver_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # todo use "with"?
    s.bind(("", 0))
    HOST = socket.gethostname()
    PORT = s.getsockname()[1]
    yield HOST, PORT
    s.listen()
    while True:
        conn, _ = s.accept()  # todo do we have to close this conn?
        infile = conn.makefile(mode="rb")  # todo do we have to close this file?
        # todo backpressure
        ds = ds_from_file(infile)

        yield ds


def ds_from_file(f):
    def gen():
        for record_batch in pyarrow.ipc.open_stream(
                f):  # todo also allow streams of pickle objects using https://stackoverflow.com/a/28745948
            for record in record_batch.to_pylist():
                yield unpack_dict(record)  # todo does this work with cycle_length>1 or do we need another threading?

    return tf.data.Dataset.from_generator(gen, output_signature=(tf.TensorSpec(
        shape=(10,), dtype=tf.float32), tf.TensorSpec(shape=(),
                                                      dtype=tf.string)))


def complete_ds():
    serv = driver_server()
    HOST, PORT = next(serv)

    serv_ds = tf.data.Dataset.from_generator(lambda: serv, output_signature=tf.data.DatasetSpec((tf.TensorSpec(
        shape=(10,), dtype=tf.float32), tf.TensorSpec(shape=(),
                                                      dtype=tf.string))))  # todo using lambda here is ugly. rather create socket outside of driver_server and pass it.

    complete_ds = serv_ds.interleave(tf.function(lambda dataset: dataset), num_parallel_calls=tf.data.AUTOTUNE,
                                     deterministic=False)  #todo could a too high cycle_length result in a deadlock?
    return HOST, PORT, complete_ds


class Pipeline(abc.ABC):
    def __init__(self):

        config = configparser.ConfigParser()
        config.read('config.ini')

        self.BUCKET_NAME = config["s3"]["BUCKET_NAME"]
        self.AWS_ACCESS_KEY_ID = config["s3"]["AWS_ACCESS_KEY_ID"]
        self.AWS_SECRET = config["s3"]["AWS_SECRET"]
        self.ENDPOINT_URL = config["s3"]["ENDPOINT_URL"]

        # deploy prebuilt dependencies according to
        # https://spark.apache.org/docs/latest/api/python/user_guide/python_packaging.html#using-virtualenv
        os.environ['PYSPARK_PYTHON'] = "./environment/bin/python"
        conf = SparkConf()
        conf.setAll([("spark.executor.instances", str(config["pyspark"]["SPARK_INSTANCES"])),
                     ("spark.yarn.dist.archives", "/pyspark_venv.tar.gz#environment")])
        self.sc = SparkContext(master="yarn", appName="web-archive-keras", conf=conf)
        self.sc.addPyFile("helpers.py")

        # self.q = NonPicklableQueue()

        # self.acc = self.sc.accumulator([], ResultsParam(self.q))
        self.acc_counter = self.sc.accumulator(collections.Counter(), CounterAccumulatorParam())

        self.BATCHSIZE = int(config["tensorflow"]["BATCHSIZE"])

        self.model = self.get_model()
        self.HOST, self.PORT, self.dataset = complete_ds()
        self.dataset = self.dataset.prefetch(tf.data.AUTOTUNE)
        self.dataset = self.batch(self.dataset, self.BATCHSIZE)

        self.dataset = self.dataset.map(self.predict, num_parallel_calls=tf.data.AUTOTUNE, deterministic=False)

        self.dataset = self.dataset.unbatch()

        self.dataset = self.dataset.filter(self.filter)

    @abc.abstractmethod
    def get_model(self):
        pass

    def batch(self, dataset, batchsize):
        return dataset.batch(batchsize)

    def start_threads(self):
        threading.Thread(target=self.feed_executors, daemon=True).start()

        def print_stats():
            while True:
                time.sleep(10)
                print(self.acc_counter)

        threading.Thread(target=print_stats, daemon=True).start()

    def run(self):
        self.start_threads()
        for data in self.dataset.as_numpy_iterator():
            self.export(*data)
            #self.acc_counter.add(collections.Counter({"n_driver_filter_passed": 1})) #todo does not work(?)

    @abc.abstractmethod
    def get_generator_factory(self):
        """
        return value is a generator that must not use any self.* attributes. Those must be copied to variables outside of the generator first#todo rework this description
        :return:
        """
        pass

    def get_bucket_files(self):
        s3_client = create_s3_client(self.AWS_ACCESS_KEY_ID, self.AWS_SECRET, self.ENDPOINT_URL)
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.BUCKET_NAME)
        return [obj['Key'] for page in pages for obj in page['Contents']]

    def feed_executors(self):
        files = self.get_bucket_files()
        rdd = self.sc.parallelize(files, len(files))
        # acc = self.acc
        generator_factory = self.get_generator_factory()
        HOST, PORT = self.HOST, self.PORT

        def node_client(generator, HOST, PORT):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((HOST, PORT))
                with s.makefile(mode="wb") as outfile:
                    writer = None
                    for record in generator:
                        batch = pyarrow.RecordBatch.from_pylist([pack_dict(record)])
                        if writer is None:
                            writer = pyarrow.ipc.new_stream(outfile,
                                                            batch.schema)  # pyarrow.schema([(0,pyarrow.float32()),(1,pyarrow.string())]))
                        writer.write_batch(batch)
                    writer.close()  # todo does this have to use a finally statement?

        rdd.foreach(lambda filename: node_client(generator_factory(filename), HOST,
                                                 PORT))  # rdd.flatMap(self.get_generator_factory()).foreach(lambda x: acc.add([x]))
        #self.q.put(None) #todo somehow stop the server

    @abc.abstractmethod
    def get_dataset(self):
        pass

    def predict(self, model_input, *args):
        """
        For the prediction, the model input is expected to be at the first position in the dataset structure
        :param model_input:
        :param args:
        :return:
        """
        prediction = self.model(model_input, training=False)
        return prediction, *args

    @abc.abstractmethod
    def filter(self, *args):
        pass

    @abc.abstractmethod
    def export(self, *args):
        pass
