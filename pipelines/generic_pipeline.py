import abc
import collections
import configparser
import os
import pickle
import socket
import threading
import time
from queue import Queue

import tensorflow as tf
from pyspark import SparkContext, SparkConf

from helpers import create_s3_client, CounterAccumulatorParam


def interleave_join(q, q2):
    while True:
        f = q.get()
        if f is None:
            q.put(None)
            return
        while True:
            try:
                q2.put(pickle.load(f))
            except EOFError:
                break


def gen(q2):
    while True:
        entry = q2.get()
        if entry is None:
            return
        yield entry


def ds_from_queue(q2, signature):
    ds = tf.data.Dataset.from_generator(lambda: gen(q2), output_signature=signature)

    # ds=ds.prefetch(tf.data.AUTOTUNE)#todo does this help?
    return ds




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

        self.acc_counter = self.sc.accumulator(collections.Counter(), CounterAccumulatorParam())

        self.BATCHSIZE = int(config["tensorflow"]["BATCHSIZE"])

        self.model = self.get_model()
        self.dataset = self.complete_ds(int(config["pyspark"]["SPARK_INSTANCES"]))
        self.dataset = self.dataset.prefetch(tf.data.AUTOTUNE)
        self.dataset = self.batch(self.dataset, self.BATCHSIZE)

        self.dataset = self.dataset.map(self.predict, num_parallel_calls=tf.data.AUTOTUNE, deterministic=False)

        self.dataset = self.dataset.unbatch()

        self.dataset = self.dataset.filter(self.filter)

    @abc.abstractmethod
    def get_model(self):
        pass
    @abc.abstractmethod
    def get_signature(self):
        pass

    def complete_ds(self, n_instances):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # todo use "with"?
        s.bind(("", 0))
        self.HOST = socket.gethostname()
        self.PORT = s.getsockname()[1]
        s.listen()
        self.q = Queue()
        self.q2 = Queue(100)  # todo make configurable

        def server():
            while True:
                conn, _ = s.accept()  # todo do we have to close this conn?
                infile = conn.makefile(mode="rb")  # todo do we have to close this file?
                # todo backpressure
                self.q.put(infile)

        threading.Thread(target=server, daemon=True).start()

        # base_ds = tf.data.Dataset.range(n_instances)

        inverleaved_ds = ds_from_queue(self.q2, self.get_signature())

        #    base_ds.interleave(lambda _: ds_from_queue(self.q,self.get_signature()),
        #                                    num_parallel_calls=tf.data.AUTOTUNE,
        #                                    deterministic=False,
        #                                    cycle_length=n_instances)

        for _ in range(n_instances):
            threading.Thread(target=lambda: interleave_join(self.q, self.q2),
                             daemon=True).start()  # todo use multiprocessing in combination with multiprocessing queues

        return inverleaved_ds

    def batch(self, dataset, batchsize):
        return dataset.batch(batchsize)

    def start_threads(self):
        threading.Thread(target=self.feed_executors, daemon=True).start()

        def print_stats():
            while True:
                time.sleep(10)
                print(self.acc_counter)  # todo prettyfy
                print("queue size:",
                      self.q2.qsize())  # todo show in percent and give advice on regulating SPARK_INSTANCES or num_GPUs

        threading.Thread(target=print_stats, daemon=True).start()

    def run(self):
        self.start_threads()
        for data in self.dataset.as_numpy_iterator():
            self.export(*data)

    @abc.abstractmethod
    def get_generator_factory(self):
        """
        return value is a generator that must not use any self.* attributes. Those must be copied to variables outside of the generator first#todo rework this description
        :return:
        """
        pass

    def get_bucket_files(self):  # todo support multiple bucket names
        s3_client = create_s3_client(self.AWS_ACCESS_KEY_ID, self.AWS_SECRET, self.ENDPOINT_URL)
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.BUCKET_NAME)
        return [obj['Key'] for page in pages for obj in page['Contents']]

    def feed_executors(self):
        files = self.get_bucket_files()
        rdd = self.sc.parallelize(files, len(files))
        generator_factory = self.get_generator_factory()
        HOST, PORT = self.HOST, self.PORT

        def node_client(generator, HOST, PORT):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((HOST, PORT))
                with s.makefile(mode="wb") as outfile:
                    for record in generator:
                        pickle.dump(record, outfile)

        rdd.foreach(lambda filename: node_client(generator_factory(filename), HOST, PORT))
        self.q.put(None)
        #todo join unpickling processes, then q2.put(None)

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
