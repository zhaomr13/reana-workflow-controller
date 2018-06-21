# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# REANA is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# REANA; if not, write to the Free Software Foundation, Inc., 59 Temple Place,
# Suite 330, Boston, MA 02111-1307, USA.
#
# In applying this license, CERN does not waive the privileges and immunities
# granted to it by virtue of its status as an Intergovernmental Organization or
# submit itself to any jurisdiction.

"""REANA Workflow Controller command line interface."""

import json
import logging
import uuid

import click
import pika
from reana_commons.database import Session
from reana_commons.models import Job, Run, RunJobs, Workflow, WorkflowStatus

from reana_workflow_controller.config import (BROKER, BROKER_PASS, BROKER_PORT,
                                              BROKER_URL, BROKER_USER)


@click.command('consume-job-queue')
def consume_job_queue():
    """Consumes job queue and updates job status."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(threadName)s - %(levelname)s: %(message)s'
    )

    job_statuses = ['submitted', 'succeeded', 'failed', 'planned']

    def _update_run_progress(workflow_uuid, msg):
        """Register succeeded Jobs to DB."""
        logging.info(
            'Updating progress for workflow {0}:\n {1}'.format(workflow_uuid,
                                                               msg))
        Session.query(Run).filter_by(workflow_uuid=workflow_uuid).\
            update({status: msg['progress'][status]['total']
                    for status in job_statuses})

    def _update_job_progress(workflow_uuid, msg):
        """Update job progress for jobs in received message."""
        current_run = Session.query(Run).filter_by(
            workflow_uuid=workflow_uuid).one_or_none()
        for status in job_statuses:
            status_progress = msg['progress'][status]
            for job_id in status_progress['job_ids']:
                Session.query(Job).filter_by(id_=job_id).\
                    update({'workflow_uuid': workflow_uuid,
                            'status': status})
                run_job = Session.query(RunJobs).filter_by(
                    run_id=current_run.id_,
                    job_id=job_id).first()
                if not run_job:
                    run_job = RunJobs()
                    run_job.id_ = uuid.uuid4()
                    run_job.run_id = current_run.id_
                    run_job.job_id = job_id
                    Session.add(run_job)
                    logging.info(
                        'Registering job {0} to run {1}.'.format(
                            job_id, current_run.id_))

    def _callback_job_status(ch, method, properties, body):
        body_dict = json.loads(body)
        workflow_uuid = body_dict.get('workflow_uuid')
        if workflow_uuid:
            status = WorkflowStatus(body_dict.get('status'))
            logging.info("Received workflow_uuid: {0} status: {1}".
                         format(workflow_uuid, status))
            logs = body_dict.get('logs') or ''
            Workflow.update_workflow_status(Session, workflow_uuid,
                                            status, logs, None)
            if 'message' in body_dict and body_dict.get('message'):
                msg = body_dict['message']
                if 'progress' in msg:
                    _update_run_progress(workflow_uuid, msg)
                    _update_job_progress(workflow_uuid, msg)
                    Session.commit()

    broker_credentials = pika.credentials.PlainCredentials(BROKER_USER,
                                                           BROKER_PASS)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(BROKER_URL,
                                  BROKER_PORT,
                                  '/',
                                  broker_credentials))
    channel = connection.channel()
    channel.queue_declare(queue='jobs-status')
    channel.basic_consume(_callback_job_status,
                          queue='jobs-status',
                          no_ack=True)
    logging.info(' [*] Waiting for messages. To exit press CTRL+C')
    channel.start_consuming()