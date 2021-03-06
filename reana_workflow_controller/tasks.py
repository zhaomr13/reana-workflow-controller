# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017 CERN.
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

"""Celery tasks definition."""

from __future__ import absolute_import

import uuid

from celery import Celery
from reana_commons.database import Session
from reana_commons.models import Job, JobCache, Run, RunJobs, Workflow
from reana_commons.utils import (calculate_file_access_time,
                                 calculate_hash_of_dir,
                                 calculate_job_input_hash)

from reana_workflow_controller.config import BROKER, POSSIBLE_JOB_STATUSES

celery = Celery('tasks',
                broker=BROKER)

celery.conf.update(CELERY_ACCEPT_CONTENT=['json'],
                   CELERY_TASK_SERIALIZER='json')


run_yadage_workflow = celery.signature('tasks.run_yadage_workflow')
run_cwl_workflow = celery.signature('tasks.run_cwl_workflow')
run_serial_workflow = celery.signature('tasks.run_serial_workflow')


def _update_workflow_status(workflow_uuid, status, logs):
    """Update workflow status in DB."""
    Workflow.update_workflow_status(Session, workflow_uuid,
                                    status, logs, None)


def _update_run_progress(workflow_uuid, msg):
    """Register succeeded Jobs to DB."""
    run = Session.query(Run).filter_by(workflow_uuid=workflow_uuid).first()
    cached_jobs = None
    if "cached" in msg['progress']:
        cached_jobs = msg['progress']['cached']
    for status in POSSIBLE_JOB_STATUSES:
        if status in msg['progress']:
            previous_total = getattr(run, status)
            if status == 'planned':
                if previous_total > 0:
                    continue
                else:
                    setattr(run, status,
                            msg['progress']['planned']['total'])
            else:
                new_total = 0
                for job_id in msg['progress'][status]['job_ids']:
                    job = Session.query(Job).\
                        filter_by(id_=job_id).one_or_none()
                    if job:
                        if job.status != status or \
                                (cached_jobs and
                                 str(job.id_) in cached_jobs['job_ids']):
                            new_total += 1
                new_total += previous_total
                setattr(run, status, new_total)
    Session.add(run)


def _update_job_progress(workflow_uuid, msg):
    """Update job progress for jobs in received message."""
    current_run = Session.query(Run).filter_by(
        workflow_uuid=workflow_uuid).one_or_none()
    for status in POSSIBLE_JOB_STATUSES:
        if status in msg['progress']:
            status_progress = msg['progress'][status]
            for job_id in status_progress['job_ids']:
                try:
                    uuid.UUID(job_id)
                except Exception:
                    continue
                Session.query(Job).filter_by(id_=job_id).\
                    update({'workflow_uuid': workflow_uuid,
                            'status': status})
                run_job = Session.query(RunJobs).filter_by(
                    run_id=current_run.id_,
                    job_id=job_id).first()
                if not run_job and current_run:
                    run_job = RunJobs()
                    run_job.id_ = uuid.uuid4()
                    run_job.run_id = current_run.id_
                    run_job.job_id = job_id
                    Session.add(run_job)


def _update_job_cache(msg):
    """Update caching information for finished job."""
    cached_job = Session.query(JobCache).filter_by(
        job_id=msg['caching_info'].get('job_id')).first()

    input_files = []
    if cached_job:
        file_access_times = calculate_file_access_time(
            msg['caching_info'].
            get('workflow_workspace').
            replace('data', 'reana/default'))
        for filename in cached_job.access_times:
            if filename in file_access_times and \
                    file_access_times[filename] > \
                    cached_job.access_times[filename]:
                input_files.append(filename)
    else:
        return
    cmd = msg['caching_info']['job_spec']['cmd']
    # removes cd to workspace, to be refactored
    clean_cmd = ';'.join(cmd.split(';')[1:])
    msg['caching_info']['job_spec']['cmd'] = clean_cmd

    if 'workflow_workspace' in msg['caching_info']['job_spec']:
        del msg['caching_info']['job_spec']['workflow_workspace']
    input_hash = calculate_job_input_hash(msg['caching_info']['job_spec'],
                                          msg['caching_info']['workflow_json'])
    workspace_hash = calculate_hash_of_dir(
        msg['caching_info'].get('workflow_workspace').
        replace('data', 'reana/default'), input_files)
    if workspace_hash == -1:
        return

    cached_job.parameters = input_hash
    cached_job.result_path = msg['caching_info'].get('result_path')
    cached_job.workspace_hash = workspace_hash
    Session.add(cached_job)
