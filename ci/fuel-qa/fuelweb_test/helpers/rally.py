#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import division

import json
import re
import os

from pipes import quote
from devops.helpers.helpers import wait
from proboscis.asserts import assert_equal
from proboscis.asserts import assert_true

from fuelweb_test import logger


class DockerContainer(object):
    def _execute_on_remote_node(self, cmd):
        return self.admin_remote.execute(cmd)

    def __init__(self, admin_remote):
        self.admin_remote = admin_remote
        self.id = None

    def run(self, image, user, bindings, env_vars, network):
        opts = ""
        if user is not None:
            opts += " --user {user}".format(user=user)
        if bindings is not None:
            for binding in bindings:
                opts += " -v {outside}:{inside}".format(outside=binding[0], inside=binding[1])
        if env_vars is not None:
            for env_var in env_vars:
                opts += " -e {var_name}={var_value}".format(var_name=env_var[0], var_value=env_var[1])
        if network is not None:
            opts += " --net={net}".format(net=network)

        cmd = "docker run -d -ti {opts} {image} /bin/bash".format(opts=opts, image=image)
        result = self._execute_on_remote_node(cmd)
        assert_equal(result["exit_code"], 0, "Failed to create container")
        result = self._execute_on_remote_node("docker ps -lq")
        assert_equal(result["exit_code"], 0, "Failed to get last container id")
        logger.debug(str(result["stdout"]))
        self.id = result["stdout"][0].strip()
        logger.debug("Container id is {id}".format(id=self.id))

    def execute(self, cmd):
        return self._execute_on_remote_node("docker exec {id} /bin/bash -c \"{cmd}\"".format(id=self.id, cmd=cmd))

    def commit(self, repotag):
        return self._execute_on_remote_node("docker commit {id} {repotag}".format(id=self.id, repotag=repotag))

    def stop(self):
        return self._execute_on_remote_node("docker stop {id}".format(id=self.id))

    def remove(self):
        return self._execute_on_remote_node("docker rm {id}".format(id=self.id))


class RallyEngine(object):
    def __init__(self,
                 admin_remote,
                 container_repo,
                 proxy_url=None,
                 user_id=0,
                 dir_for_home='/var/rally_home',
                 home_bind_path='/home/rally'):
        self.admin_remote = admin_remote
        self.container_repo = container_repo
        self.repository_tag = 'latest'
        self.proxy_url = proxy_url or ""
        self.user_id = user_id
        self.dir_for_home = dir_for_home
        self.home_bind_path = home_bind_path
        self.rally_plugins_dir = "/opt/rally/plugins"
        self.rally_container = DockerContainer(admin_remote)
        self.setup()

    def image_exists(self, tag='latest'):
        cmd = "docker images | awk 'NR > 1{print $1\" \"$2}'"
        logger.debug('Checking Docker images...')
        result = self.admin_remote.execute(cmd)
        logger.debug(result)
        existing_images = [line.strip().split() for line in result['stdout']]
        return [self.container_repo, tag] in existing_images

    def pull_image(self):
        # TODO(apanchenko): add possibility to load image from local path or
        # remote link provided in settings, in order to speed up downloading
        cmd = 'docker pull {0}'.format(self.container_repo)
        logger.debug('Downloading Rally repository/image from registry...')
        result = self.admin_remote.execute(cmd)
        logger.debug(result)
        return self.image_exists()

    def setup_utils(self):
        utils = ['gawk', 'vim', 'curl', 'firefox', 'python-pip', 'xvfb']
        cmd = ('unset http_proxy https_proxy; apt-get update; apt-get install -y {0}'.format(' '.join(utils)))
        logger.debug('Installing utils "{0}" to the Rally container...'.format(utils))
        result = self.rally_container.execute(cmd)
        assert_equal(result['exit_code'], 0,
                     'Utils installation failed in Rally container: '
                     '{0}'.format(result))
        cmd = "pip install pyvirtualdisplay selenium xvfbwrapper"
        result = self.rally_container.execute(cmd)
        assert_equal(result['exit_code'], 0,
                     'Pip packages installation failed in Rally container: '
                     '{0}'.format(result))
        cmd = ("wget https://github.com/mozilla/geckodriver/releases/download/v0.10.0/geckodriver-v0.10.0-linux64.tar.gz; "
               "tar zxf geckodriver-v0.10.0-linux64.tar.gz; "
               "sudo mv geckodriver /usr/local/bin/")

        result = self.rally_container.execute(cmd)
        assert_equal(result['exit_code'], 0,
                     'Chrome driver installation failed in Rally container: '
                     '{0}'.format(result))

    def upload_rally_plugins(self):
        work_dir = os.environ.get("WORKSPACE", "./")
        base_dir = os.path.join(work_dir, "fuelweb_test/rally/plugins")
        files = os.listdir(base_dir)

        self.rally_container.execute("mkdir /opt/rally/plugins")
        self.admin_remote.upload(base_dir, self.rally_plugins_dir)

    def init_rally_container(self):
        bindings = [(self.dir_for_home, self.home_bind_path),
                    (self.rally_plugins_dir, self.rally_plugins_dir)]
        env_vars = [("http_proxy", self.proxy_url),
                    ("https_proxy", self.proxy_url)]
        self.rally_container.run("{0}:{1}".format(self.container_repo, self.repository_tag),
                                 "0",
                                 bindings,
                                 env_vars,
                                 "host")
    def create_database(self):
        # (mchernik) TODO: Add support for Postgresql and don't create 
        # new DB in this case
        check_rally_db_cmd = 'test -s .rally.sqlite'
        result = self.rally_container.execute(check_rally_db_cmd)
        if result['exit_code'] == 0:
            return
        logger.debug('Recreating Database for Rally...')
        create_rally_db_cmd = 'rally-manage db recreate'
        result = self.rally_container.execute(create_rally_db_cmd)
        assert_equal(result['exit_code'], 0,
                     'Rally Database creation failed: {0}!'.format(result))
        result = self.rally_container.execute(check_rally_db_cmd)
        assert_equal(result['exit_code'], 0, 'Failed to create Database for '
                                             'Rally: {0} !'.format(result))

    def prepare_image(self):
        self.init_rally_container()

        self.create_database()
        self.setup_utils()
        self.upload_rally_plugins()

        self.rally_container.stop()
        self.rally_container.commit("{0}:{1}".format(self.container_repo, "ready"))
        self.rally_container.remove()

        return self.image_exists(tag='ready')

    def setup_bash_alias(self):
        alias_name = 'rally_docker'
        check_alias_cmd = '. /root/.bashrc && alias {0}'.format(alias_name)
        result = self.admin_remote.execute(check_alias_cmd)
        if result['exit_code'] == 0:
            return
        logger.debug('Creating bash alias for Rally inside container...')
        create_alias_cmd = ("alias {alias_name}='docker run --user {user_id} "
                            "--net=\"host\"  -e \"http_proxy={proxy_url}\" -t "
                            "-i -v {dir_for_home}:{home_bind_path}  "
                            "{container_repo}:{tag} rally'".format(
            alias_name=alias_name,
            user_id=self.user_id,
            proxy_url=self.proxy_url,
            dir_for_home=self.dir_for_home,
            home_bind_path=self.home_bind_path,
            container_repo=self.container_repo,
            tag=self.repository_tag))
        result = self.admin_remote.execute('echo "{0}">> /root/.bashrc'.format(
            create_alias_cmd))
        assert_equal(result['exit_code'], 0,
                     "Alias creation for running Rally from container failed: "
                     "{0}.".format(result))
        result = self.admin_remote.execute(check_alias_cmd)
        assert_equal(result['exit_code'], 0,
                     "Alias creation for running Rally from container failed: "
                     "{0}.".format(result))

    def setup(self):
        if not self.image_exists():
            assert_true(self.pull_image(),
                        "Docker image for Rally not found!")
        if not self.image_exists(tag='ready'):
            assert_true(self.prepare_image(),
                        "Docker image for Rally is not ready!")
        self.repository_tag = 'ready'

        # init one more time but now using other image with 'ready' tag
        self.init_rally_container()

        result = self.rally_container.execute("rally deployment list")
        logger.debug(str(result["stdout"]))

        # self.setup_bash_alias()

    def list_deployments(self):
        cmd = (r"rally deployment list | awk -F "
               r"'[[:space:]]*\\\\|[[:space:]]*' '/\ydeploy\y/{print \$2}'")
        result = self.rally_container.execute(cmd)
        
        return [line.strip() for line in result['stdout']]

    def show_deployment(self, deployment_uuid):
        cmd = ("rally deployment show {} | grep -v ^\\+ | tr -d \\| ").format(deployment_uuid)
        result = self.rally_container.execute(cmd)
        assert_equal(len(result['stdout']), 2,
                     "Command 'rally deployment show' returned unexpected "
                     "value: expected 2 lines, got {0}: ".format(result))
        header, deployment  = result['stdout']
        return dict(zip(header.strip().split(), deployment.strip().split()))

    def list_tasks(self):
        cmd = "rally task list --uuids-only"
        result = self.rally_container.execute(cmd)
        logger.debug('Rally tasks list: {0}'.format(result))
        return [line.strip() for line in result['stdout']]

    def get_task_status(self, task_uuid):
        cmd = "rally task status {0}".format(task_uuid)
        result = self.rally_container.execute(cmd)
        assert_equal(result['exit_code'], 0,
                     "Getting Rally task status failed: {0}".format(result))
        task_status = ''.join(result['stdout']).strip().split()[-1]
        logger.debug('Rally task "{0}" has status "{1}".'.format(task_uuid,
                                                                 task_status))
        return task_status


class RallyDeployment(object):
    def __init__(self, rally_engine, cluster_vip, username, password, tenant,
                 key_port=5000, proxy_url='', force_create=False):
        self.rally_engine = rally_engine
        self.cluster_vip = cluster_vip
        self.username = username
        self.password = password
        self.tenant_name = tenant
        self.keystone_port = str(key_port)
        self.proxy_url = proxy_url
        self.auth_url = "http://{0}:{1}/v2.0/".format(self.cluster_vip,
                                                      self.keystone_port)
        self.set_proxy = not self.is_proxy_set
        self._uuid = None
        self.create_deployment(force_create)
        result = self.rally_engine.rally_container.execute("rally deployment list")
        logger.debug(str(result))

    @property
    def uuid(self):
        if self._uuid is None:
            for d_uuid in self.rally_engine.list_deployments():
                deployment = self.rally_engine.show_deployment(d_uuid)
                logger.debug("Deployment info: {0}".format(deployment))
                if self.auth_url in deployment['auth_url'] and \
                                self.username == deployment['username'] and \
                                self.tenant_name == deployment['tenant_name']:
                    self._uuid = d_uuid
                    break
        return self._uuid

    @property
    def is_proxy_set(self):
        cmd = '[ "${{http_proxy}}" == "{0}" ]'.format(self.proxy_url)
        return self.rally_engine.rally_container.execute(cmd)['exit_code'] == 0

    @property
    def is_deployment_exist(self):
        return self.uuid is not None

    def create_deployment(self, force):
        if self.is_deployment_exist and not force:
            logger.info('Deployment already exists, skipping creation')
            return
        deployment = {}
        deployment['admin'] = {
            'username'    : self.username,
            'password'    : self.password,
            'tenant_name' : self.tenant_name,
        }
        deployment['auth_url'] = self.auth_url
        deployment['endpoint'] = 'null'
        deployment['type'] = 'ExistingCloud'
        deployment['https_insecure'] = True
        cmd = "echo '{}' > depl.conf".format(json.dumps(deployment))
        cmd = cmd.replace('"', '\\""')
        logger.info(cmd)
        aaa = self.rally_engine.rally_container.execute(cmd)
        logger.info(aaa)
        ##cmd = "echo '{{ \\\"admin\\\": {{ \\\"password\\\": \\\"{pwd}\\\", \\\"tenant_name\\\": \\\"{ten}\\\", \\\"username\\\": \\\"{usr}\\\" }}, \\\"auth_url\\\": \\\"{auth}\\\", \\\"endpoint\\\": null, \\\"type\\\": \\\"ExistingCloud\\\", \\\"https_insecure\\\": true }}' > depl.conf".format(pwd=self.password, ten=self.tenant_name, usr=self.username, auth=self.auth_url)
        cmd = 'rally deployment create --name {0} --filename depl.conf'.format(self.cluster_vip)
        aaa = self.rally_engine.rally_container.execute(cmd)
        logger.info(aaa)
        logger.info(self.uuid)
        self.check_deployment(self.uuid)


    def check_deployment(self, deployment_uuid=''):
        cmd = 'rally deployment check {0}'.format(deployment_uuid)
        result = self.rally_engine.rally_container.execute(cmd)
        if result['exit_code'] == 0:
            return True
        else:
            logger.error('Rally deployment check failed: {0}'.format(result))
            return False


class RallyTask(object):
    def __init__(self, rally_deployment, scenario, rally_args):
        self.deployment = rally_deployment
        self.engine = self.deployment.rally_engine
        self.scenario = scenario
        self.uuid = None
        self._status = None
        self.rally_args = rally_args

    @property
    def status(self):
        if self.uuid is None:
            self._status = None
        else:
            self._status = self.engine.get_task_status(self.uuid)
        return self._status

    def start(self):
        log_file = '{0}_results.tmp.log'.format(self.scenario)
        cmd = ('rally task start {}  --task-args-file rally_args.json &> {}'.format(self.scenario, log_file)
        )
        result = self.engine.rally_container.execute(cmd)
        if result['exit_code']:
           logger.warn('Scenario {} failed to start!'.format(self.scenario))
           return log_file

        logger.info('Started Rally task: {0}'.format(result))
        cmd = ("awk 'BEGIN{{retval=1}};/^Using task:/{{print $NF; retval=0}};"
               "END {{exit retval}}' {0}").format(log_file)
        wait(lambda: self.engine.rally_container.execute(cmd)['exit_code'] == 0,
             timeout=30, timeout_msg='Rally task {!r} creation timeout'
                                     ''.format(result))
        result = self.engine.rally_container.execute(cmd)
        m = re.match("Using task: ([a-z0-9-]+)", result["stdout"][0])
        assert_true(m is not None, "Cannot find task id")
        task_uuid = m.group(1)
        logger.debug("!!! task_uuid = {}".format(task_uuid))
        tasks = self.engine.list_tasks()
        logger.debug("!!! tasks = {}".format(str(tasks)))
        assert_true(task_uuid in tasks,
                    "Rally task creation failed: {0}".format(result))
        self.uuid = task_uuid
        return log_file

    def abort(self, task_id):
        logger.debug('Stop Rally task {0}'.format(task_id))
        cmd = 'rally task abort {0}'.format(task_id)
        self.engine.rally_container.execute(cmd)
        assert_true(
            self.status in ('finished', 'aborted'),
            "Rally task {0} was not aborted; current task status "
            "is {1}".format(task_id, self.status))

    def get_results(self):
        if self.status == 'finished':
            cmd = 'rally task results {0}'.format(self.uuid)
            result = self.engine.rally_container.execute(cmd)
            assert_equal(result['exit_code'], 0,
                         "Getting task results failed: {0}".format(result))
            logger.debug("Rally task {0} result: {1}".format(self.uuid,
                                                             result))
            return ''.join(result['stdout'])


class RallyResult(object):
    def __init__(self, json_results):
        self.values = {
            'full_duration': 0.00,
            'load_duration': 0.00,
            'errors': 0
        }
        self.raw_data = []
        self.parse_raw_results(json_results)

    def parse_raw_results(self, raw_results):
        data = json.loads(raw_results)
        assert_equal(len(data), 1,
                     "Current implementation of RallyResult class doesn't "
                     "support results with length greater than '1'!")
        self.raw_data = data[0]
        self.values['full_duration'] = data[0]['full_duration']
        self.values['load_duration'] = data[0]['load_duration']
        self.values['errors'] = sum([len(result['error'])
                                     for result in data[0]['result']])


class RallyBenchmarkTest(object):
    def __init__(self, container_repo, environment, cluster_id,
                 test_type, rally_args):
        self.admin_remote = environment.d_env.get_admin_remote()
        self.cluster_vip = environment.fuel_web.get_mgmt_vip(cluster_id)
        self.cluster_credentials = \
            environment.fuel_web.get_cluster_credentials(cluster_id)
        self.proxy_url = environment.fuel_web.get_alive_proxy(cluster_id)
        logger.debug('Rally proxy URL is: {0}'.format(self.proxy_url))
        self.container_repo = container_repo
        self.home_dir = 'rally-{0}'.format(cluster_id)
        self.test_type = test_type
        self.rally_args = rally_args
        self.engine = RallyEngine(
            admin_remote=self.admin_remote,
            container_repo=self.container_repo,
            proxy_url=self.proxy_url,
            dir_for_home='/var/{0}/'.format(self.home_dir)
        )
        self.deployment = RallyDeployment(
            rally_engine=self.engine,
            cluster_vip=self.cluster_vip,
            username=self.cluster_credentials['username'],
            password=self.cluster_credentials['password'],
            tenant=self.cluster_credentials['tenant'],
            proxy_url=self.proxy_url,
            force_create=True
        )
        self.current_task = None

    def run(self, timeout=60 * 10, result=True):
        self.current_task = RallyTask(self.deployment, self.test_type, self.rally_args)
        logger.info('Starting Rally benchmark test...')
        self.current_task.start()
        return self.current_task.get_results()
        # assert_equal(self.current_task.status, 'running',
        #              'Rally task was started, but it is not running, status: '
        #              '{0}'.format(self.current_task.status))
        # if result:
        #     wait(lambda: self.current_task.status == 'finished',
        #          timeout=timeout, timeout_msg='Rally benchmark test timeout')
        #     logger.info('Rally benchmark test is finished.')
        #     return RallyResult(json_results=self.current_task.get_results())
