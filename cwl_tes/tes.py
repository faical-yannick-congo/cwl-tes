import os
import shutil
import logging

from cwltool.draft2tool import CommandLineTool
from cwltool.errors import WorkflowException
from cwltool.pathmapper import PathMapper
from cwltool.stdfsaccess import StdFsAccess
from cwltool.workflow import defaultMakeTool

from pipeline import Pipeline, PipelineJob
from poll import PollThread
from pprint import pformat

from schema_salad.ref_resolver import file_uri

try:
    import requests
except ImportError:
    pass

log = logging.getLogger('tes-backend')


class TESService:

    def __init__(self, addr):
        if not addr.startswith("http"):
            addr = "http://" + addr

        if addr.endswith("/"):
            addr = addr[:-1]

        self.addr = addr

    def submit(self, task):
        r = requests.post(url='%s/v1/tasks' % (self.addr), json=task)
        r.raise_for_status()
        data = r.json()
        return data['id']

    def get_job(self, task_id):
        r = requests.get(url='%s/v1/tasks/%s' % (self.addr, task_id),
                         params={'view': 'MINIMAL'})
        r.raise_for_status()
        return r.json()


class TESPipeline(Pipeline):

    def __init__(self, url, kwargs):
        super(TESPipeline, self).__init__()
        self.kwargs = kwargs
        self.service = TESService(url)
        if kwargs.get('basedir') is not None:
            self.basedir = kwargs.get('basedir')
        else:
            self.basedir = os.getcwd()
        self.fs_access = StdFsAccess(self.basedir)

    def make_exec_tool(self, spec, **kwargs):
        return TESPipelineTool(spec, self, fs_access=self.fs_access, **kwargs)

    def make_tool(self, spec, **kwargs):
        if 'class' in spec and spec['class'] == 'CommandLineTool':
            return self.make_exec_tool(spec, **kwargs)
        else:
            return defaultMakeTool(spec, **kwargs)


class TESPipelineTool(CommandLineTool):

    def __init__(self, spec, pipeline, fs_access, **kwargs):
        super(TESPipelineTool, self).__init__(spec, **kwargs)
        self.spec = spec
        self.pipeline = pipeline
        self.fs_access = fs_access

    def makeJobRunner(self, use_container=True):
        return TESPipelineJob(self.spec, self.pipeline, self.fs_access)

    def makePathMapper(self, reffiles, stagedir, **kwargs):
        return PathMapper(reffiles, kwargs['basedir'], stagedir)


class TESPipelineJob(PipelineJob):

    def __init__(self, spec, pipeline, fs_access):
        super(TESPipelineJob, self).__init__(spec, pipeline)
        self.outputs = None
        self.docker_workdir = '/var/spool/cwl'
        self.fs_access = fs_access

    def create_input_parameter(self, name, d):
        if 'contents' in d:
            return {
                'name': name,
                'description': 'cwl_input:%s' % (name),
                'path': d['path'],
                'contents': d['contents'],
                'type': d['class'].upper()
            }
        else:
            return {
                'name': name,
                'description': 'cwl_input:%s' % (name),
                'url': d['location'],
                'path': d['path'],
                'type': d['class'].upper()
            }

    def parse_job_order(self, k, v, inputs):
        if isinstance(v, dict):
            if all([i in v for i in ['location', 'path', 'class']]):
                inputs.append(self.create_input_parameter(k, v))

                if 'secondaryFiles' in v:
                    for f in v['secondaryFiles']:
                        self.parse_job_order(f['basename'], f, inputs)

            else:
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        self.parse_job_order(sk, sv, inputs)

                    else:
                        break

        elif isinstance(v, list):
            for i in range(len(v)):
                if isinstance(v[i], dict):
                    self.parse_job_order("%s[%s]" % (k, i), v[i], inputs)

                else:
                    break

        return inputs

    def collect_input_parameters(self):
        inputs = []
        for k, v in self.joborder.items():
            self.parse_job_order(k, v, inputs)

        # manage InitialWorkDirRequirement
        for listing in self.generatefiles['listing']:
            loc = self.fs_access.join(self.tmpdir, listing['basename'])
            with self.fs_access.open(loc, 'wb') as gen:
                if 'contents' in listing:
                    gen.write(listing['contents'])
                else:
                    loc = listing['location']
            parameter = {
                'name': listing['basename'],
                'description': 'InitialWorkDirRequirement:cwl_input:%s' % (listing['basename']),
                'url': file_uri(loc),
                'path': self.fs_access.join(self.docker_workdir, listing['basename'])
            }
            inputs.append(parameter)

        return inputs

    def create_task(self):
        input_parameters = self.collect_input_parameters()
        output_parameters = []

        if self.stdout is not None:
            parameter = {
                'name': 'stdout',
                'url': self.output2url(self.stdout),
                'path': self.output2path(self.stdout)
            }
            output_parameters.append(parameter)

        if self.stderr is not None:
            parameter = {
                'name': 'stderr',
                'url': self.output2url(self.stderr),
                'path': self.output2path(self.stderr)
            }
            output_parameters.append(parameter)

        output_parameters.append({
            'name': 'workdir',
            'url': self.output2url(''),
            'path': self.docker_workdir,
            'type': 'DIRECTORY'
        })

        container = self.find_docker_requirement()

        cpus = None
        ram = None
        disk = None
        for i in self.requirements:
            if i.get('class', 'NA') == 'ResourceRequirement':
                cpus = i.get('coresMin', i.get('coresMax', None))
                ram = i.get('ramMin', i.get('ramMax', None))
                disk = i.get('outdirMin', i.get('outdirMax', None))
            elif i.get('class', 'NA') == 'DockerRequirement':
                if i.get('dockerOutputDirectory', None) is not None:
                    output_parameters.append({
                        'name': 'dockerOutputDirectory',
                        'url': self.output2url(''),
                        'path': i.get('dockerOutputDirectory'),
                        'type': 'DIRECTORY'
                    })

        resources = {}
        if cpus is not None:
            resources['cpu_cores'] = cpus

        if ram is not None:
            resources['ram_gb'] = ram

        if disk is not None:
            resources['size_gb'] = disk

        create_body = {
            'name': self.name,
            'description': self.spec.get('doc', ''),
            'executors': [{
                'cmd': self.command_line,
                'image_name': container,
                'workdir': self.docker_workdir,
                'stdout': self.output2path(self.stdout),
                'stderr': self.output2path(self.stderr),
                'stdin': self.stdin,
                'environ': self.environment
            }],
            'inputs': input_parameters,
            'outputs': output_parameters,
            'resources': resources,
            'tags': {'CWLDocumentId': self.spec.get('id')}
        }

        return create_body

    def run(self, pull_image=True, rm_container=True, rm_tmpdir=True,
            move_outputs='move', **kwargs):
        # useful for debugging
        # log.debug('[job %s] self.__dict__ from run() ----------------------' % (self.name))
        # log.debug(pformat(self.__dict__))

        task = self.create_task()

        log.debug('[job %s] CREATED TASK MSG----------------------' % (self.name))
        log.debug(pformat(task))

        try:
            task_id = self.pipeline.service.submit(task)
            log.debug('[job %s] SUBMITTED TASK ----------------------' % (self.name))
            log.debug('[job %s] task id: %s ' % (self.name, task_id))
            operation = self.pipeline.service.get_job(task_id)
        except Exception as e:
            log.error(u"[job %s] Failed to submit task to TES service:\n%s" % (self.name, e))
            return WorkflowException(e)

        def callback(operation):
            try:
                self.outputs = self.collect_outputs(self.outdir)
                self.output_callback(self.outputs, 'success')
            except WorkflowException as e:
                log.error(u"[job %s] Job error:\n%s" % (self.name, e))
                self.output_callback({}, 'permanentFail')
            except Exception as e:
                log.exception("Exception while running job")
                self.output_callback({}, 'permanentFail')
            finally:
                if self.outputs is not None:
                    log.debug('[job %s] OUTPUTS ------------------' % (self.name))
                    log.debug(pformat(self.outputs))
                self.cleanup(rm_tmpdir)

        poll = TESPipelinePoll(
            jobname=self.name,
            service=self.pipeline.service,
            operation=operation,
            callback=callback
        )

        self.pipeline.add_thread(poll)
        poll.start()

    def cleanup(self, rm_tmpdir):
        log.debug('[job %s] STARTING CLEAN UP ------------------' % (self.name))
        if self.stagedir and os.path.exists(self.stagedir):
            log.debug('[job %s] Removing input staging directory %s', self.name, self.stagedir)
            shutil.rmtree(self.stagedir, True)

        if rm_tmpdir:
            log.debug('[job %s] Removing temporary directory %s', self.name, self.tmpdir)
            shutil.rmtree(self.tmpdir, True)

    def output2url(self, path):
        if path is not None:
            return file_uri(self.fs_access.join(self.outdir, os.path.basename(path)))
        return None

    def output2path(self, path):
        if path is not None:
            return self.fs_access.join(self.docker_workdir, path)
        return None


class TESPipelinePoll(PollThread):

    def __init__(self, jobname, service, operation, callback):
        super(TESPipelinePoll, self).__init__(operation)
        self.name = jobname
        self.service = service
        self.callback = callback

    def poll(self):
        return self.service.get_job(self.operation['id'])

    def is_done(self, operation):
        terminal_states = ['COMPLETE', 'CANCELED', 'ERROR', 'SYSTEM_ERROR']
        if 'state' in operation:
            if operation['state'] in terminal_states:
                log.debug('[job %s] JOB %s ------------------' %
                          (self.name, operation['state'])
                )
                return True
        return False

    def complete(self, operation):
        self.callback(operation)
