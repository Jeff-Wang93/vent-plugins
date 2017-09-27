#!/usr/bin/env python
#
#   Copyright (c) 2016 In-Q-Tel, Inc, All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""
This example is a test of a usable API demonstrating the test and documentation
workflow for the code base.

Created on 17 May 2016
@author: Charlie Lewis, dgrossman
"""
import json
import Queue
import signal
import sys
import threading
import time
from functools import partial
from os import getenv

import requests
import schedule
import random

from poseidon.baseClasses.Logger_Base import Logger
from poseidon.baseClasses.Rabbit_Base import Rabbit_Base
from poseidon.poseidonMonitor.Config.Config import config_interface
from poseidon.poseidonMonitor.NorthBoundControllerAbstraction.NorthBoundControllerAbstraction import \
    controller_interface

ENDPOINT_STATES = [('K', 'KNOWN'), ('U', 'UNKNOWN'), ('M', 'MIRRORING'),
                   ('S', 'SHUTDOWN'), ('R', 'REINVESTIGATING')]

module_logger = Logger

CTRL_C = False


def schedule_job_kickurl(func, logger):
    logger.debug('kick')
    func.NorthBoundControllerAbstraction.get_endpoint(
        'Update_Switch_State').update_endpoint_state()


def rabbit_callback(ch, method, properties, body, q=None):
    ''' callback, places rabbit data into internal queue'''
    module_logger.logger.debug('got a message: {0}:{1}:{2}'.format(
        method.routing_key, body, type(body)))
    # TODO more
    if q is not None:
        q.put((method.routing_key, body))
    else:
        module_logger.logger.debug('posedionMain workQueue is None')


def schedule_thread_worker(schedule, logger):
    global CTRL_C
    logLine = 'starting thread_worker'
    logger.debug(logLine)
    while not CTRL_C:
        schedule.run_pending()
        logLine = 'scheduler woke {0}'.format(
            threading.current_thread().getName())
        time.sleep(1)
        logger.debug(logLine)
    logger.debug('Threading stop:{0}'.format(
        threading.current_thread().getName()))


def start_investigating():
    pass


def schedule_job_reinvestigation(max_investigations, endpoints, logger):
    ostr = 'reinvestagtion time'
    logger.debug(ostr)
    logger.debug('endpoints:{0}'.format(endpoints))
    candidates = []

    currently_investigating = 0
    for my_hash, my_value in endpoints.iteritems():
        if 'state' in my_value:
            if my_value['state'] == 'REINVESTIGATING' or my_value['next-state'] == 'REINVESTIGATING':
                currently_investigating += 1
            elif my_value['state'] == 'KNOWN':
                candidates.append(my_hash)

    # get random order of things that are known
    random.shuffle(candidates)

    if currently_investigating < max_investigations:
        ostr = 'room to investigate'
        logger.debug(ostr)
        for x in range(max_investigations - currently_investigating):
            if len(candidates) >= 1:
                chosen = candidates.pop()
                ostr = 'starting investigation {0}:{1}'.format(x, chosen)
                logger.debug(ostr)
                endpoints[chosen]['next-state'] = 'REINVESTIGATING'
                start_investigating()
    else:
        ostr = 'investigators all busy'
        logger.debug(ostr)


class Monitor(object):

    def __init__(self, skip_rabbit):
        # get the logger setup
        self.logger = module_logger.logger
        self.mod_configuration = dict()
        module_logger.logger_config(None)

        self.mod_name = self.__class__.__name__
        self.skip_rabbit = skip_rabbit

        # timer class to call things periodically in own thread
        self.schedule = schedule

        # rabbit
        self.rabbit_channel_local = None
        self.rabbit_chanel_connection_local = None
        self.rabbit_thread = None

        self.actions = dict()
        self.Config = config_interface
        self.Config.set_owner(self)
        self.NorthBoundControllerAbstraction = controller_interface
        self.NorthBoundControllerAbstraction.set_owner(self)

        self.configSelf()

        # set the logger level
        module_logger.set_level(self.mod_configuration['logger_level'])

        # wire up handlers for Config
        self.logger.debug('handler Config')

        # check
        self.Config.configure()
        self.Config.first_run()
        self.Config.configure_endpoints()

        self.m_queue = Queue.Queue()

        # wire up handlers for NorthBoundControllerAbstraction
        self.logger.debug('handler NorthBoundControllerAbstraction')

        # check
        self.NorthBoundControllerAbstraction.configure()
        self.NorthBoundControllerAbstraction.first_run()
        self.NorthBoundControllerAbstraction.configure_endpoints()

        # make a shortcut
        self.uss = self.NorthBoundControllerAbstraction.get_endpoint(
            'Update_Switch_State')

        self.logger.debug('----------------------')
        self.init_logging()

        scan_frequency = int(self.mod_configuration['scan_frequency'])
        self.schedule.every(scan_frequency).seconds.do(
            partial(schedule_job_kickurl, func=self, logger=self.logger))

        reinvestigation_frequency = int(
            self.mod_configuration['reinvestigation_frequency'])
        max_concurrent_reinvestigations = int(
            self.mod_configuration['max_concurrent_reinvestigations'])

        self.schedule.every(reinvestigation_frequency).seconds.do(
            partial(schedule_job_reinvestigation,
                    max_investigations=max_concurrent_reinvestigations,
                    endpoints=self.NorthBoundControllerAbstraction.get_endpoint(
                        'Update_Switch_State').endpoint_states,
                    logger=self.logger))

        self.schedule_thread = threading.Thread(
            target=partial(
                schedule_thread_worker,
                schedule=self.schedule,
                logger=self.logger),
            name='st_worker')

    def print_endpoint_state(self, endpoint_states):
        def same_old(logger, state, letter, endpoint_states):
            logger.debug('*******{0}*********'.format(state))

            out_flag = False
            for my_hash in endpoint_states.keys():
                my_dict = endpoint_states[my_hash]
                if my_dict['state'] == state:
                    out_flag = True
                    logger.debug('{0}:{1}:{2}->{3}:{4}'.format(letter,
                                                               my_hash,
                                                               my_dict['state'],
                                                               my_dict['next-state'],
                                                               my_dict['endpoint']))
            if not out_flag:
                logger.debug('None')

        for l, s in ENDPOINT_STATES:
            same_old(self.logger, s, l, endpoint_states)

            self.logger.debug('****************')

    def init_logging(self):
        ''' setup logging  '''
        config = None

        path = getenv('loggingFile')

        if path is None:
            path = self.mod_configuration.get('loggingFile')

        if path is not None:
            with open(path, 'rt') as f:
                config = json.load(f)
        module_logger.logger_config(config)

    def configSelf(self):
        ''' get configuraiton for this module '''
        conf = self.Config.get_endpoint('Handle_SectionConfig')
        for item in conf.direct_get(self.mod_name):
            k, v = item
            self.mod_configuration[k] = v
        ostr = '{0}:config:{1}'.format(self.mod_name, self.mod_configuration)
        self.logger.debug(ostr)

    def update_next_state(self, rabbit_transitions):
        next_state = None
        current_state = None
        endpoint_states = self.uss.return_endpoint_state()
        for my_hash in endpoint_states.keys():
            my_dict = endpoint_states[my_hash]
            current_state = my_dict['state']
            if current_state == 'UNKNOWN':
                my_dict['next-state'] = 'MIRRORING'
        for my_hash in rabbit_transitions.keys():
            my_dict = endpoint_states[my_hash]
            current_state = my_dict['state']
            my_dict['next-state'] = rabbit_transitions[my_hash]

    def start_vent_collector(self, dev_hash, num_captures=1):
        '''
        Given a device hash and optionally a number of captures
        to be taken, starts vent collector for that device with the
        options specified in poseidon.config.
        '''
        try:
            payload = {
                'nic': self.mod_configuration['collector_nic'],
                'id': dev_hash,
                'interval': self.mod_configuration['collector_interval'],
                'filter': '\'host {0}\''.format(
                    self.uss.get_endpoint_ip(dev_hash)),
                'iters': str(num_captures)}
            self.logger.debug('vent payload: ' + str(payload))
            vent_addr = self.mod_configuration[
                'vent_ip'] + ':' + self.mod_configuration['vent_port']
            uri = 'http://' + vent_addr + '/create'
            resp = requests.post(uri, json=payload)
            self.logger.debug('collector repsonse: ' + resp.text)
        except Exception as e:
            self.logger.debug('failed to start vent collector' + str(e))

    def get_rabbit_message(self, item):
        self.logger.debug('rabbit_message:{1}'.format(item))
        routing_key, my_obj = item
        my_obj = json.loads(my_obj)
        ret_val = {}
        self.logger.debug('routing_key:{0}'.format(routing_key))
        if routing_key is not None and routing_key == 'poseidon.algos.ML.results':
            self.logger.debug('value:{0}'.format(my_obj))
        # TODO do something with reccomendation
        return ret_val

    def process(self):
        global CTRL_C
        signal.signal(signal.SIGINT, partial(self.signal_handler))
        while not CTRL_C:
            self.logger.debug('***************CTRL_C:{0}'.format(CTRL_C))
            time.sleep(1)
            self.logger.debug('woke from sleeping')
            found_work, item = self.get_q_item()
            rabbit_transitions = {}

            # plan out the transitions
            if found_work:
                # TODO make this read until nothing in q
                rabbit_transitions = self.get_rabbit_message(item)

            eps = self.uss.return_endpoint_state()

            state_transitions = self.update_next_state(rabbit_transitions)

            self.print_endpoint_state(eps)

            # make the transitions

            for endpoint_hash in eps.keys():
                current_state = eps[endpoint_hash]['state']
                next_state = eps[endpoint_hash]['next-state']

                # dont do anything
                if next_state == 'NONE':
                    continue

                if next_state == 'MIRRORING':
                    self.logger.debug(
                        'updating:{0}:{1}->{2}'.format(endpoint_hash, current_state, next_state))
                    self.logger.debug('*********** U NOTIFY VENT ***********')
                    self.start_vent_collector(endpoint_hash)
                    self.logger.debug('*********** U MIRROR PORT ***********')
                    self.uss.mirror_endpoint(endpoint_hash)
                if next_state == 'REINVESTIGATING':
                    self.logger.debug(
                        'updating:{0}:{1}->{2}'.format(endpoint_hash, current_state, next_state))
                    self.logger.debug('*********** R NOTIFY VENT ***********')
                    self.start_vent_collector(endpoint_hash)
                    self.logger.debug('*********** R MIRROR PORT ***********')
                    self.uss.mirror_endpoint(endpoint_hash)

    def get_q_item(self):
        found_work = False
        item = None

        try:
            item = self.m_queue.get(False)
            found_work = True
        except Queue.Empty:
            pass

        return (found_work, item)

    def signal_handler(self, signal, frame):
        global CTRL_C
        CTRL_C = True
        self.logger.debug('=================CTRLC{0}'.format(CTRL_C))
        for job in self.schedule.jobs:
            self.logger.debug('CTRLC:{0}'.format(job))
            self.schedule.cancel_job(job)


def main(skip_rabbit=False):
    ''' main function '''
    pmain = Monitor(skip_rabbit=skip_rabbit)
    if not skip_rabbit:
        rabbit = Rabbit_Base()
        host = pmain.mod_configuration['rabbit-server']
        port = int(pmain.mod_configuration['rabbit-port'])
        exchange = 'topic-poseidon-internal'
        queue_name = 'poseidon_main'
        binding_key = ['poseidon.algos.#', 'poseidon.action.#']
        retval = rabbit.make_rabbit_connection(
            host, port, exchange, queue_name, binding_key)
        pmain.rabbit_channel_local = retval[0]
        pmain.rabbit_channel_connection_local = retval[1]
        pmain.rabbit_thread = rabbit.start_channel(
            pmain.rabbit_channel_local,
            rabbit_callback,
            'poseidon_main',
            pmain.m_queue)
        # def start_channel(self, channel, callback, queue):
        pmain.schedule_thread.start()

    # loop here until told not to
    pmain.process()

    pmain.logger.debug('SHUTTING DOWN')
    pmain.rabbit_channel_connection_local.close()
    pmain.rabbit_channel_local.close()
    pmain.logger.debug('EXITING')
    sys.exit(0)


if __name__ == '__main__':
    main(skip_rabbit=False)
