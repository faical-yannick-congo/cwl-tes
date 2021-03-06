from __future__ import print_function

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import yaml


def cmd_exists(program):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return True
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return True

    return False


def popen(*args, **kwargs):
    kwargs['preexec_fn'] = os.setsid
    return subprocess.Popen(*args, **kwargs)


def kill(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait()
    except OSError:
        pass


def temp_config(dir, config):
    configFile = tempfile.NamedTemporaryFile(dir=dir, mode='w', delete=False)
    yaml.dump(config, configFile)
    return configFile


def config_seconds(sec):
    # The funnel config is currently parsed as nanoseconds
    # this helper makes that manageale
    return int(sec * 1000000000)


class SimpleServerTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = None
        self.task_server = None
        self.addCleanup(self.cleanup)

        if not cmd_exists("funnel"):
            print(
                "-bash: funnel: command not found\n",
                "see https://ohsu-comp-bio.github.io/funnel/install/",
                "for instuctions on how to install",
                file=sys.stdout
            )
            raise RuntimeError

        self.rootprojectdir = os.path.dirname(os.path.dirname(
            os.path.realpath(__file__)
        ))

        self.testdir = os.path.join(self.rootprojectdir, "tests")
        if not os.path.exists(os.path.join(self.testdir, "test_tmp")):
            os.mkdir(os.path.join(self.testdir, "test_tmp"))
        self.tmpdir = tempfile.mkdtemp(
            dir=os.path.join(self.testdir, "test_tmp"),
            prefix="conformance_test_v1.0_"
        )
        os.environ['TMPDIR'] = self.tmpdir

        f, db_path = tempfile.mkstemp(dir=self.tmpdir, prefix="tes_task_db.")
        os.close(f)
        funnel_work_dir = os.path.join(self.tmpdir, "funnel-work-dir")
        os.mkdir(funnel_work_dir)
        logFile = os.path.join(self.tmpdir, "funnel_log.txt")

        # Build server config file (YAML)
        rate = config_seconds(0.05)
        configFile = temp_config(dir=self.tmpdir, config={
            "HostName": "localhost",
            "HTTPPort": "8000",
            "RPCPort": "9090",
            "DBPath": db_path,
            "WorkDir": funnel_work_dir,
            "Storage": {
                "Local": {
                    "AllowedDirs": [self.testdir]
                }
            },
            "LogLevel": "debug",
            "LogPath": logFile,
            "Worker": {
                "Timeout": -1,
                "StatusPollRate": rate,
                "LogUpdateRate": rate,
                "NewJobPollRate": rate,
                "UpdateRate": rate,
                "TrackerRate": rate
            },
            "ScheduleRate": rate,
        })

        # Start server
        cmd = ["funnel", "server", "--config", configFile.name]
        logging.info("Running %s" % (" ".join(cmd)))
        self.task_server = popen(cmd)
        signal.signal(signal.SIGINT, self.cleanup)
        time.sleep(1)

    # We're using this instead of tearDown because python doesn't call tearDown
    # if setUp fails. Since our setUp is complex, that means things don't get
    # properly cleaned up (e.g. processes are orphaned).
    def cleanup(self, *args):
        if self.task_server is not None:
            kill(self.task_server)
