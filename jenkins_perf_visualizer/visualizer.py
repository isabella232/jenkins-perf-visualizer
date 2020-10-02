#!/usr/bin/python

"""Emit a flamechart that shows where time is spent during a deploy.

The output is a chart.  The x axis is seconds since the beginning
of the input jenkins job until the end.  (If multiple jobs are specified
it's the beginning of the first job until the end of the last one.)

The y axis is a set of "nodes".  A node captures the time taken by
a block of Jenkins pipeline steps:
   1) Every stage() step begins a new node, labeled with the name of
      the stage, and includes all the commands in that stage.
   2) Every branch of a parallel() step begins a new node, labeled
      with the name of that branch, and includes all the commands
      run by that branch.
   3) "<jenkins job name>" holds the overall jenkins job.
(Note that our helper jenkins functions, like notify() define
some stages of their own, so every Jenkins job has a "main"
node which contains everything inside a "notify" block.)

Each node can be in one of several states:
1) Running
2) Sleeping, via a sleep(), waitFor(), or prompt() jenkins step.
   Note that sleeping via shell (`sh("sleep 10")`) is *not*
   counted here -- we have no way of knowing what sh() is doing!
3) Waiting for an executor.  This happens when a node decides to
   start running on a jenkins worker, and it has to wait for a
   new gce machine to get started up.

Our output for each node is a horizontal bar, saying what the
node is doing during the given second.  Each node has its own
color, with varying brightnesses to say what the node is doing
(so if the coordinate (50, 6) is light yellow, it means that node
#6 is waiting for an executor during second 50, whereas if it's
dark yellow it's running during second 50).

This script works in three main stages:
1) It figures out what steps were run, and how long they took,
   by scraping the html from a jenkins /flowGraphTable
   ("Pipeline steps") page, and collects them into a runtime tree.
   (The `Step` class.)
2) It figures out which Node each part of that tree is running
   over (by looking for stage() and parallel() steps), and
   linearizes the steps in a Node into a set of time-ranges
   where that node is doing something.  It then looks at each
   of the steps in that node to categorize each moment of time
   into a category: running, sleeping, or waiting.
   (The `Node` class.)
3) It constructs and emits a graph based on the node data.
   (`create_html()`.)

# TODO(csilvers): rename 'timerange' into a better name
# TODO(csilvers): rename 'job' to 'build' when appropriate
"""
from __future__ import absolute_import

import json
import os
import re
import time
import webbrowser

import builds
import fetch
import jenkins
import nodes
import steps


# TODO(csilvers): move to a config file
KEEPER_RECORD_ID = 'mHbUyJXAmnZyqLY3pMUmjQ'


def create_html(job_datas):
    """Return an html page that will render our flame-like chart.

    We use custom CSS to do this.
    """
    deploy_start_time_ms = min(j.data['jobStartTimeMs'] for j in job_datas)
    deploy_end_time_ms = max(j.data['jobEndTimeMs'] for j in job_datas)
    title = ('%s (%s)'
             % (' + '.join(sorted(set(j.data['title'] for j in job_datas))),
                time.strftime("%Y/%m/%d %H:%M:%S",
                              time.localtime(deploy_start_time_ms / 1000))))
    deploy_data = {
        'jobs': [j.data for j in job_datas],
        'title': title,
        'colors': builds.COLORS,
        'deployStartTimeMs': deploy_start_time_ms,
        'deployEndTimeMs': deploy_end_time_ms,
    }

    visualizer_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(visualizer_dir, 'visualize.html')) as f:
        template = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.js')) as f:
        js = f.read()
    with open(os.path.join(visualizer_dir, 'visualize.css')) as f:
        css = f.read()

    return template \
        .replace('{{js}}', js) \
        .replace('{{css}}', css) \
        .replace('{{data}}', json.dumps(deploy_data, sort_keys=True, indent=2))


def main(buildses, output_dir, jenkins_username=None, jenkins_password=None):
    """jenkins_* vars are not needed if all builds are .data files."""
    job_datas = []
    for build in buildses:
        if build.endswith('.data'):  # Used a cached file to avoid the network
            (job, build_id) = os.path.basename(build[:-len('.data')]).replace(
                '--', '/').split(':')
            with open(build, 'rb') as f:
                step_html = f.read().decode('utf-8')
            m = re.search(r'<script>var parameters = (.*?)</script>',
                          step_html)
            job_params = json.loads(m.group(1) if m else '{}')
            # We get the job-start time by the file's mtime.
            job_start_time = os.path.getmtime(build)
            outfile = build
        else:
            if jenkins_password:
                jenkins_client = jenkins.get_client_via_password(
                    jenkins_username, jenkins_password)
            else:
                jenkins_client = jenkins.get_client_via_keeper(
                    KEEPER_RECORD_ID)
            (job, build_id) = build.split(':')
            (step_html, job_params, job_start_time, outfile) = (
                fetch.fetch_build(job, build_id, output_dir, jenkins_client))

        step_root = steps.parse_pipeline_steps(step_html)
        node_root = nodes.steps_to_nodes(step_root)
        job_datas.append(builds.BuildData(
            job, build_id, job_start_time, job_params, node_root))

    job_datas.sort(key=lambda jd: jd.job_start_time_ms)

    html_file = outfile.replace('.data', '.html')
    html = create_html(job_datas)
    with open(html_file, 'wb') as f:
        f.write(html.encode('utf-8'))
    webbrowser.open(html_file)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'build', nargs='+',
        help=("Jenkins builds to fetch, e.g. deploy/build-webapp:1543 "
              "OR a json-filename like deploy-build-webapp:1543.json."))
    parser.add_argument('--jenkins-username',
                        default='jenkins@khanacademy.org')
    parser.add_argument('--jenkins-pw',
                        help=('API token that gives access to job data. '
                              'If not set, fetch the secret from keeper '
                              '(record %s)' % KEEPER_RECORD_ID))
    parser.add_argument('-d', '--output-dir',
                        default='/tmp/jenkins-job-perf-analysis',
                        help='Directory to write the flamechart output file')

    args = parser.parse_args()

    try:
        main(args.build, args.output_dir,
             args.jenkins_username, args.jenkins_pw)
    except Exception:
        import pdb
        import sys
        import traceback
        extype, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)
