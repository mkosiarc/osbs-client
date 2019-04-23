"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals
import shutil
import os
import json
from osbs.api import OSBS
from osbs.constants import (DEFAULT_ARRANGEMENT_VERSION,
                            ORCHESTRATOR_INNER_TEMPLATE,
                            WORKER_INNER_TEMPLATE,
                            SECRETS_PATH,
                            ORCHESTRATOR_OUTER_TEMPLATE)
from osbs import utils
from osbs.repo_utils import RepoInfo, ModuleSpec
from osbs.build.build_request import BuildRequest
from osbs.build.plugins_configuration import PluginsConfiguration
from tests.constants import (TEST_GIT_URI,
                             TEST_GIT_REF,
                             TEST_GIT_BRANCH,
                             TEST_COMPONENT,
                             TEST_VERSION,
                             TEST_FILESYSTEM_KOJI_TASK_ID,
                             INPUTS_PATH)
from tests.fake_api import openshift, osbs, get_pulp_additional_config  # noqa:F401
from tests.test_api import request_as_response
from tests.build_.test_build_request import (get_plugins_from_build_json,
                                             get_plugin,
                                             plugin_value_get,
                                             NoSuchPluginException)
from flexmock import flexmock
import pytest


# Copied from atomic_reactor.constants
# Can't import directly, because atomic_reactor depends on osbs-client and therefore
# osbs-client can't dpeend on atomic_reactor.
# Don't want to put these in osbs.constants and then have atomic_reactor import them,
# because then atomic_reactor could break in weird ways if run with the wrong version
# of osbs-client
# But we need to verify the input json against the actual keys, so keeping this list
# up to date is the best solution.
PLUGIN_KOJI_PROMOTE_PLUGIN_KEY = 'koji_promote'
PLUGIN_KOJI_IMPORT_PLUGIN_KEY = 'koji_import'
PLUGIN_KOJI_UPLOAD_PLUGIN_KEY = 'koji_upload'
PLUGIN_KOJI_TAG_BUILD_KEY = 'koji_tag_build'
PLUGIN_PULP_PUBLISH_KEY = 'pulp_publish'
PLUGIN_PULP_PUSH_KEY = 'pulp_push'
PLUGIN_PULP_SYNC_KEY = 'pulp_sync'
PLUGIN_PULP_PULL_KEY = 'pulp_pull'
PLUGIN_PULP_TAG_KEY = 'pulp_tag'
PLUGIN_ADD_FILESYSTEM_KEY = 'add_filesystem'
PLUGIN_FETCH_WORKER_METADATA_KEY = 'fetch_worker_metadata'
PLUGIN_GROUP_MANIFESTS_KEY = 'group_manifests'
PLUGIN_BUILD_ORCHESTRATE_KEY = 'orchestrate_build'
PLUGIN_KOJI_PARENT_KEY = 'koji_parent'
PLUGIN_COMPARE_COMPONENTS_KEY = 'compare_components'
PLUGIN_CHECK_AND_SET_PLATFORMS_KEY = 'check_and_set_platforms'
PLUGIN_REMOVE_WORKER_METADATA_KEY = 'remove_worker_metadata'
PLUGIN_RESOLVE_COMPOSES_KEY = 'resolve_composes'
PLUGIN_VERIFY_MEDIA_KEY = 'verify_media'
PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY = 'export_operator_manifests'

OSBS_WITH_PULP_PARAMS = {
    'platform_descriptors': None,
    'additional_config': get_pulp_additional_config(),
    'kwargs': {'registry_uri': 'registry.example.com/v2'}}


class ArrangementBase(object):
    ARRANGEMENT_VERSION = None
    COMMON_PARAMS = {}
    DEFAULT_PLUGINS = {}
    ORCHESTRATOR_ADD_PARAMS = {}
    WORKER_ADD_PARAMS = {}

    def mock_env(self, base_image='fedora23/python'):
        class MockParser(object):
            labels = {
                'name': 'fedora23/something',
                'com.redhat.component': TEST_COMPONENT,
                'version': TEST_VERSION,
            }
            baseimage = base_image

        class MockConfiguration(object):
            container = {
                'compose': {
                    'modules': ['mod_name:mod_stream:mod_version']
                }
            }

            module = container['compose']['modules'][0]
            container_module_specs = [ModuleSpec.from_str(module)]
            depth = 0

            def is_autorebuild_enabled(self):
                return False

        (flexmock(utils)
            .should_receive('get_repo_info')
            .with_args(TEST_GIT_URI, TEST_GIT_REF, git_branch=TEST_GIT_BRANCH, depth=None)
            .and_return(RepoInfo(MockParser(), MockConfiguration())))

        # Trick create_orchestrator_build into return the *request* JSON
        flexmock(OSBS, _create_build_config_and_build=request_as_response)
        flexmock(OSBS, _create_scratch_build=request_as_response)

    def get_plugins_from_buildrequest(self, build_request, template=None):
        return build_request.inner_template

    @pytest.mark.parametrize('template', [  # noqa:F811
        ORCHESTRATOR_INNER_TEMPLATE,
        WORKER_INNER_TEMPLATE,
    ])
    def test_running_order(self, osbs, template):
        """
        Verify the plugin running order.

        This is to catch tests missing from these test classes when a
        plugin is added.
        """

        inner_template = template.format(
            arrangement_version=self.ARRANGEMENT_VERSION,
        )
        build_request = osbs.get_build_request(inner_template=inner_template,
                                               arrangement_version=self.ARRANGEMENT_VERSION)
        plugins = self.get_plugins_from_buildrequest(build_request, template)
        phases = ('prebuild_plugins',
                  'buildstep_plugins',
                  'prepublish_plugins',
                  'postbuild_plugins',
                  'exit_plugins')
        actual = {}
        for phase in phases:
            actual[phase] = [plugin['name']
                             for plugin in plugins.get(phase, {})]

        assert actual == self.DEFAULT_PLUGINS[template]

    def get_build_request(self, build_type, osbs,  # noqa:F811
                          additional_params=None):
        self.mock_env(base_image=additional_params.get('base_image'))
        params = self.COMMON_PARAMS.copy()
        assert build_type in ('orchestrator', 'worker')
        if build_type == 'orchestrator':
            params.update(self.ORCHESTRATOR_ADD_PARAMS)
            fn = osbs.create_orchestrator_build
        elif build_type == 'worker':
            params.update(self.WORKER_ADD_PARAMS)
            fn = osbs.create_worker_build

        params.update(additional_params or {})
        params['arrangement_version'] = self.ARRANGEMENT_VERSION
        return params, fn(**params).json

    def get_orchestrator_build_request(self, osbs,  # noqa:F811
                                       additional_params=None):
        return self.get_build_request('orchestrator', osbs, additional_params)

    def get_worker_build_request(self, osbs,  # noqa:F811
                                 additional_params=None):
        return self.get_build_request('worker', osbs, additional_params)

    def assert_plugin_not_present(self, build_json, phase, name):
        plugins = get_plugins_from_build_json(build_json)
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, phase, name)

    def get_pulp_sync_registry(self, conf):
        """Return the docker registry used by pulp content sync."""
        for registry_uri in conf.get_registry_uris():
            registry = utils.RegistryURI(registry_uri)
            if registry.version == 'v2':
                return registry.docker_uri


class TestArrangementV1(ArrangementBase):
    """
    This class tests support for the oldest supported arrangement
    version, 1.

    NOTE! When removing this test class, *make sure* that any methods
    it provides for the test class for the next oldest supported
    arrangement version are copied across to that test class.
    """

    ARRANGEMENT_VERSION = 1

    COMMON_PARAMS = {
        'git_uri': TEST_GIT_URI,
        'git_ref': TEST_GIT_REF,
        'git_branch': TEST_GIT_BRANCH,
        'user': 'john-foo',
        'component': TEST_COMPONENT,
        'openshift_uri': 'http://openshift/',
    }

    ORCHESTRATOR_ADD_PARAMS = {
        'platforms': ['x86_64'],
    }

    WORKER_ADD_PARAMS = {
        'platform': 'x86_64',
        'release': 1,
    }

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                'reactor_config',
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
            ],

            'exit_plugins': [
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                PLUGIN_ADD_FILESYSTEM_KEY,
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_by_labels',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                PLUGIN_PULP_SYNC_KEY,
                'compress',
                PLUGIN_PULP_PULL_KEY,
            ],

            'exit_plugins': [
                'delete_from_registry',  # not tested
                PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,  # not tested
                'store_metadata_in_osv3',  # not tested
                'koji_tag_build',  # not tested
                'sendmail',  # not tested
                'remove_built_image',  # not tested
            ],
        },
    }

    @pytest.mark.parametrize('build_type', [  # noqa:F811
        'orchestrator',
        'worker',
    ])
    @pytest.mark.parametrize('scratch', [False, True])
    @pytest.mark.parametrize('base_image, expect_plugin', [
        ('koji/image-build', False),
        ('foo', True),
    ])
    def test_pull_base_image(self, osbs, build_type, scratch,
                             base_image, expect_plugin):
        phase = 'prebuild_plugins'
        plugin = 'pull_base_image'
        additional_params = {
            'base_image': base_image,
        }
        if scratch:
            additional_params['scratch'] = True

        (params, build_json) = self.get_build_request(build_type,
                                                      osbs,
                                                      additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not expect_plugin:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, phase, plugin)
        else:
            args = plugin_value_get(plugins, phase, plugin, 'args')

            allowed_args = set([
                'parent_registry',
                'parent_registry_insecure',
            ])
            assert set(args.keys()) <= allowed_args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image', ['koji/image-build', 'foo'])
    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)
    def test_delete_from_registry(self, osbs, base_image, scratch):
        phase = 'exit_plugins'
        plugin = 'delete_from_registry'
        additional_params = {
            'base_image': base_image,
        }
        if scratch:
            additional_params['scratch'] = True

        (params, build_json) = self.get_build_request('worker',
                                                      osbs,
                                                      additional_params)
        plugins = get_plugins_from_build_json(build_json)
        args = plugin_value_get(plugins, phase, plugin, 'args')
        allowed_args = set([
            'registries',
        ])
        assert set(args.keys()) <= allowed_args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image, expect_plugin', [
        ('koji/image-build', True),
        ('foo', False)
    ])
    def test_add_filesystem_in_worker(self, osbs, base_image, scratch,
                                      expect_plugin):
        additional_params = {
            'base_image': base_image,
            'yum_repourls': ['https://example.com/my.repo'],
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_worker_build_request(osbs,
                                                           additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not expect_plugin:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'prebuild_plugins', PLUGIN_ADD_FILESYSTEM_KEY)
        else:
            args = plugin_value_get(plugins, 'prebuild_plugins',
                                    PLUGIN_ADD_FILESYSTEM_KEY, 'args')

            allowed_args = set([
                'koji_hub',
                'repos',
                'architecture',
            ])
            assert set(args.keys()) <= allowed_args
            assert 'koji_hub' in args
            assert args['repos'] == params['yum_repourls']


class TestArrangementV2(TestArrangementV1):
    """
    Differences from arrangement version 1:
    - add_filesystem runs with different parameters
    - add_filesystem also runs in orchestrator build
    - koji_parent runs in orchestrator build
    """

    ARRANGEMENT_VERSION = 2

    WORKER_ADD_PARAMS = {
        'platform': 'x86_64',
        'release': 1,
        'filesystem_koji_task_id': TEST_FILESYSTEM_KOJI_TASK_ID,
    }

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                PLUGIN_ADD_FILESYSTEM_KEY,
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                PLUGIN_KOJI_PARENT_KEY,
                'reactor_config',
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
            ],

            'exit_plugins': [
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                PLUGIN_ADD_FILESYSTEM_KEY,
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_by_labels',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                PLUGIN_PULP_SYNC_KEY,
                'compress',
                PLUGIN_PULP_PULL_KEY,
            ],

            'exit_plugins': [
                'delete_from_registry',
                PLUGIN_KOJI_PROMOTE_PLUGIN_KEY,
                'store_metadata_in_osv3',
                'koji_tag_build',
                'sendmail',
                'remove_built_image',
            ],
        },
    }

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image, expect_plugin', [
        ('koji/image-build', True),
        ('foo', False)
    ])
    def test_add_filesystem_in_orchestrator(self, osbs, base_image, scratch,
                                            expect_plugin):
        additional_params = {
            'base_image': base_image,
            'yum_repourls': ['https://example.com/my.repo'],
        }
        if scratch:
            additional_params['scratch'] = True

        (params,
         build_json) = self.get_orchestrator_build_request(osbs,
                                                           additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not expect_plugin:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'prebuild_plugins', PLUGIN_ADD_FILESYSTEM_KEY)
        else:
            args = plugin_value_get(plugins, 'prebuild_plugins',
                                    PLUGIN_ADD_FILESYSTEM_KEY, 'args')
            allowed_args = set([
                'koji_hub',
                'repos',
                'architectures',
            ])
            assert set(args.keys()) <= allowed_args
            assert 'koji_hub' in args
            assert args['repos'] == params['yum_repourls']
            assert args['architectures'] == params['platforms']

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image, expect_plugin', [
        ('koji/image-build', True),
        ('foo', False)
    ])
    def test_add_filesystem_in_worker(self, osbs, base_image, scratch,
                                      expect_plugin):
        additional_params = {
            'base_image': base_image,
            'yum_repourls': ['https://example.com/my.repo'],
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_worker_build_request(osbs,
                                                           additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not expect_plugin:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'prebuild_plugins', PLUGIN_ADD_FILESYSTEM_KEY)
        else:
            args = plugin_value_get(plugins, 'prebuild_plugins',
                                    PLUGIN_ADD_FILESYSTEM_KEY, 'args')
            allowed_args = set([
                'koji_hub',
                'repos',
                'from_task_id',
                'architecture',
            ])
            assert set(args.keys()) <= allowed_args
            assert 'koji_hub' in args
            assert args['repos'] == params['yum_repourls']
            assert args['from_task_id'] == params['filesystem_koji_task_id']

    @pytest.mark.parametrize(('scratch', 'base_image', 'expect_plugin'), [  # noqa:F811
        (True, 'koji/image-build', False),
        (True, 'foo', False),
        (False, 'koji/image-build', False),
        (False, 'foo', True),
    ])
    def test_koji_parent_in_orchestrator(self, osbs, base_image, scratch,
                                         expect_plugin):
        additional_params = {
            'base_image': base_image,
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not expect_plugin:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'prebuild_plugins', PLUGIN_KOJI_PARENT_KEY)
        else:
            args = plugin_value_get(plugins, 'prebuild_plugins',
                                    PLUGIN_KOJI_PARENT_KEY, 'args')
            allowed_args = set([
                'koji_hub',
            ])
            assert set(args.keys()) <= allowed_args
            assert 'koji_hub' in args


class TestArrangementV3(TestArrangementV2):
    """
    Differences from arrangement version 2:
    - fetch_worker_metadata, koji_import, koji_tag_build, sendmail,
      check_and_set_rebuild, run in the orchestrator build
    - koji_upload runs in the worker build
    - koji_promote does not run
    """

    ARRANGEMENT_VERSION = 3

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                PLUGIN_ADD_FILESYSTEM_KEY,
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                PLUGIN_KOJI_PARENT_KEY,
                'reactor_config',
                'check_and_set_rebuild',
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
                PLUGIN_FETCH_WORKER_METADATA_KEY,
            ],

            'exit_plugins': [
                'delete_from_registry',
                PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                'koji_tag_build',
                'store_metadata_in_osv3',
                'sendmail',
                'remove_built_image',
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                PLUGIN_ADD_FILESYSTEM_KEY,
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_by_labels',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                PLUGIN_PULP_SYNC_KEY,
                'compress',
                PLUGIN_PULP_PULL_KEY,
                PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
            ],

            'exit_plugins': [
                'delete_from_registry',
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },
    }

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    def test_koji_upload(self, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
            'koji_upload_dir': 'upload',
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_worker_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'postbuild_plugins', PLUGIN_KOJI_UPLOAD_PLUGIN_KEY)
            return

        args = plugin_value_get(plugins, 'postbuild_plugins',
                                         PLUGIN_KOJI_UPLOAD_PLUGIN_KEY, 'args')

        match_args = {
            'blocksize': 10485760,
            'build_json_dir': 'inputs',
            'koji_keytab': False,
            'koji_principal': False,
            'koji_upload_dir': 'upload',
            'kojihub': 'http://koji.example.com/kojihub',
            'url': '/',
            'use_auth': False,
            'verify_ssl': False,
            'platform': 'x86_64',
        }
        assert match_args == args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    def test_koji_import(self, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
            'koji_upload_dir': 'upload',
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'exit_plugins', PLUGIN_KOJI_IMPORT_PLUGIN_KEY)
            return

        args = plugin_value_get(plugins, 'exit_plugins',
                                         PLUGIN_KOJI_IMPORT_PLUGIN_KEY, 'args')

        match_args = {
            'koji_keytab': False,
            'kojihub': 'http://koji.example.com/kojihub',
            'url': '/',
            'use_auth': False,
            'verify_ssl': False
        }
        assert match_args == args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    def test_fetch_worker_metadata(self, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'postbuild_plugins', PLUGIN_FETCH_WORKER_METADATA_KEY)
            return

        args = plugin_value_get(plugins, 'postbuild_plugins',
                                         PLUGIN_FETCH_WORKER_METADATA_KEY, 'args')

        match_args = {}
        assert match_args == args

    @pytest.mark.parametrize('triggers', [False, True])  # noqa:F811
    def test_check_and_set_rebuild(self, tmpdir, osbs, triggers):

        imagechange = [
            {
                "type": "ImageChange",
                "imageChange": {
                    "from": {
                        "kind": "ImageStreamTag",
                        "name": "{{BASE_IMAGE_STREAM}}"
                    }
                }
            }
        ]

        if triggers:
            orch_outer_temp = ORCHESTRATOR_INNER_TEMPLATE.format(
                arrangement_version=self.ARRANGEMENT_VERSION
            )
            for basename in [ORCHESTRATOR_OUTER_TEMPLATE, orch_outer_temp]:
                shutil.copy(os.path.join(INPUTS_PATH, basename),
                            os.path.join(str(tmpdir), basename))

            with open(os.path.join(str(tmpdir), ORCHESTRATOR_OUTER_TEMPLATE), 'r+') as orch_json:
                build_json = json.load(orch_json)
                build_json['spec']['triggers'] = imagechange

                orch_json.seek(0)
                json.dump(build_json, orch_json)
                orch_json.truncate()

            flexmock(osbs.os_conf, get_build_json_store=lambda: str(tmpdir))
            (flexmock(BuildRequest)
                .should_receive('adjust_for_repo_info')
                .and_return(True))

        additional_params = {
            'base_image': 'fedora:latest',
        }
        params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if not triggers:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'prebuild_plugins', 'check_and_set_rebuild')
            return

        args = plugin_value_get(plugins, 'prebuild_plugins',
                                         'check_and_set_rebuild', 'args')

        match_args = {
            "label_key": "is_autorebuild",
            "label_value": "true",
            "url": "/",
            "verify_ssl": False,
            'use_auth': False,
        }
        assert match_args == args


class TestArrangementV4(TestArrangementV3):
    """
    Orchestrator build differences from arrangement version 3:
    - tag_from_config enabled
    - pulp_tag enabled
    - pulp_sync enabled
    - pulp_sync takes an additional "publish":false argument
    - pulp_publish enabled
    - pulp_pull enabled
    - group_manifests enabled

    Worker build differences from arrangement version 3:
    - tag_from_config takes "tag_suffixes" argument
    - tag_by_labels disabled
    - pulp_push takes an additional "publish":false argument
    - pulp_sync disabled
    - pulp_pull disabled
    - delete_from_registry disabled
    """

    ARRANGEMENT_VERSION = 4

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'reactor_config',
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                PLUGIN_KOJI_PARENT_KEY,
                'check_and_set_rebuild',
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
                PLUGIN_FETCH_WORKER_METADATA_KEY,
                PLUGIN_COMPARE_COMPONENTS_KEY,
                'tag_from_config',
                PLUGIN_GROUP_MANIFESTS_KEY,
                PLUGIN_PULP_TAG_KEY,
                PLUGIN_PULP_SYNC_KEY,
            ],

            'exit_plugins': [
                PLUGIN_PULP_PUBLISH_KEY,
                PLUGIN_PULP_PULL_KEY,
                'delete_from_registry',
                PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                'koji_tag_build',
                'store_metadata_in_osv3',
                'sendmail',
                'remove_built_image',
                PLUGIN_REMOVE_WORKER_METADATA_KEY,
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
                'flatpak_create_oci',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                'compress',
                PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
            ],

            'exit_plugins': [
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },
    }

    @pytest.mark.parametrize(('params', 'build_type', 'has_plat_tag',  # noqa:F811
                              'has_primary_tag'), (
        ({}, 'orchestrator', False, True),
        ({'scratch': True}, 'orchestrator', False, False),
        ({'platform': 'x86_64'}, 'worker', True, False),
        ({'platform': 'x86_64', 'scratch': True}, 'worker', True, False),
    ))
    def test_tag_from_config(self, osbs, params, build_type, has_plat_tag, has_primary_tag):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        additional_params.update(params)
        _, build_json = self.get_build_request(build_type, osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'postbuild_plugins', 'tag_from_config', 'args')

        assert set(args.keys()) == set(['tag_suffixes'])
        assert set(args['tag_suffixes'].keys()) == set(['unique', 'primary'])

        unique_tags = args['tag_suffixes']['unique']
        assert len(unique_tags) == 1
        unique_tag_suffix = ''
        if has_plat_tag:
            unique_tag_suffix = '-' + additional_params.get('platform')
        assert unique_tags[0].endswith(unique_tag_suffix)

        primary_tags = args['tag_suffixes']['primary']
        if has_primary_tag:
            assert set(primary_tags) == set(['latest', '{version}', '{version}-{release}'])

    @pytest.mark.parametrize('osbs', [  # noqa:F811
        {'platform_descriptors': {'x86_64': {'enable_v1': True}},
            'additional_config': get_pulp_additional_config(),
            'kwargs': {'registry_uri': 'registry.example.com/v2'}}], indirect=True)
    def test_pulp_push(self, osbs):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_worker_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'postbuild_plugins', PLUGIN_PULP_PUSH_KEY, 'args')

        build_conf = osbs.build_conf
        # Use first docker registry and strip off /v2
        pulp_registry_name = build_conf.get_pulp_registry()
        pulp_secret_path = '/'.join([SECRETS_PATH, build_conf.get_pulp_secret()])

        expected_args = {
            'pulp_registry_name': pulp_registry_name,
            'pulp_secret_path': pulp_secret_path,
            'load_exported_image': True,
            'dockpulp_loglevel': 'INFO',
            'publish': False
        }

        assert args == expected_args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('osbs, use_pulp', [
        ({'platform_descriptors': None, 'additional_config': None, 'kwargs': None}, False),
        (OSBS_WITH_PULP_PARAMS, True)], indirect=['osbs'])
    def test_koji_upload(self, use_pulp, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
            'koji_upload_dir': 'upload',
        }
        if scratch:
            additional_params['scratch'] = True

        params, build_json = self.get_worker_build_request(osbs,
                                                           additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, 'postbuild_plugins', PLUGIN_KOJI_UPLOAD_PLUGIN_KEY)
            return

        args = plugin_value_get(plugins, 'postbuild_plugins',
                                         PLUGIN_KOJI_UPLOAD_PLUGIN_KEY, 'args')

        match_args = {
            'blocksize': 10485760,
            'build_json_dir': 'inputs',
            'koji_keytab': False,
            'koji_principal': False,
            'koji_upload_dir': 'upload',
            'kojihub': 'http://koji.example.com/kojihub',
            'url': '/',
            'use_auth': False,
            'verify_ssl': False,
            'platform': 'x86_64',
        }

        if use_pulp:
            match_args['report_multiple_digests'] = True

        assert match_args == args

    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)  # noqa:F811
    def test_pulp_tag(self, osbs):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'postbuild_plugins', PLUGIN_PULP_TAG_KEY, 'args')
        build_conf = osbs.build_conf
        pulp_registry_name = build_conf.get_pulp_registry()
        pulp_secret_path = '/'.join([SECRETS_PATH, build_conf.get_pulp_secret()])

        expected_args = {
            'pulp_registry_name': pulp_registry_name,
            'pulp_secret_path': pulp_secret_path,
            'dockpulp_loglevel': 'INFO',
        }

        assert args == expected_args

    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)  # noqa:F811
    def test_pulp_sync(self, osbs):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'postbuild_plugins', PLUGIN_PULP_SYNC_KEY, 'args')

        build_conf = osbs.build_conf
        docker_registry = self.get_pulp_sync_registry(build_conf)
        pulp_registry_name = build_conf.get_pulp_registry()
        pulp_secret_path = '/'.join([SECRETS_PATH, build_conf.get_pulp_secret()])
        expected_args = {
            'docker_registry': docker_registry,
            'pulp_registry_name': pulp_registry_name,
            'pulp_secret_path': pulp_secret_path,
            'dockpulp_loglevel': 'INFO',
            'publish': False
        }

        assert args == expected_args

    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)  # noqa:F811
    def test_pulp_publish(self, osbs):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'exit_plugins', PLUGIN_PULP_PUBLISH_KEY, 'args')
        build_conf = osbs.build_conf
        pulp_registry_name = build_conf.get_pulp_registry()
        pulp_secret_path = '/'.join([SECRETS_PATH, build_conf.get_pulp_secret()])

        expected_args = {
            'pulp_registry_name': pulp_registry_name,
            'pulp_secret_path': pulp_secret_path,
            'dockpulp_loglevel': 'INFO',
        }

        assert args == expected_args

    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)  # noqa:F811
    def test_pulp_pull(self, osbs):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'exit_plugins', PLUGIN_PULP_PULL_KEY, 'args')
        expected_args = {'insecure': True}
        assert args == expected_args

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image', ['koji/image-build', 'foo'])
    @pytest.mark.parametrize('osbs', [OSBS_WITH_PULP_PARAMS], indirect=True)
    def test_delete_from_registry(self, osbs, base_image, scratch):
        phase = 'exit_plugins'
        plugin = 'delete_from_registry'
        additional_params = {
            'base_image': base_image,
        }
        if scratch:
            additional_params['scratch'] = True

        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)
        args = plugin_value_get(plugins, phase, plugin, 'args')

        docker_registry = self.get_pulp_sync_registry(osbs.build_conf)
        assert args == {'registries': {docker_registry: {'insecure': True}}}

    @pytest.mark.parametrize('osbs, with_group', [  # noqa:F811
        ({'platform_descriptors': {'x86_64': {'architecture': 'amd64'}},
            'additional_config': get_pulp_additional_config(),
            'kwargs': {'registry_uri': 'registry.example.com/v2'}}, False),
        ({'platform_descriptors': {'x86_64': {'architecture': 'amd64'}},
            'additional_config': get_pulp_additional_config(with_group=True),
            'kwargs': {'registry_uri': 'registry.example.com/v2'}}, True)],
        indirect=['osbs'])
    def test_group_manifests(self, osbs, with_group):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'postbuild_plugins', PLUGIN_GROUP_MANIFESTS_KEY, 'args')
        docker_registry = self.get_pulp_sync_registry(osbs.build_conf)

        expected_args = {
            'goarch': {'x86_64': 'amd64'},
            'group': with_group,
            'registries': {docker_registry: {'insecure': True, 'version': 'v2'}}
        }
        assert args == expected_args

    @pytest.mark.parametrize('build_type', (  # noqa:F811
        'orchestrator',
        'worker',
    ))
    def test_inject_parent_image(self, osbs, build_type):
        additional_params = {
            'base_image': 'foo',
            'koji_parent_build': 'fedora-26-9',
        }
        _, build_json = self.get_build_request(build_type, osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'prebuild_plugins', 'inject_parent_image', 'args')
        expected_args = {
            'koji_parent_build': 'fedora-26-9',
            'koji_hub': osbs.build_conf.get_kojihub()
        }
        assert args == expected_args

    @pytest.mark.parametrize('worker', [False, True])  # noqa:F811
    @pytest.mark.parametrize('scratch', [False, True])
    def test_flatpak(self, osbs, worker, scratch):
        additional_params = {
            'flatpak': True,
            'target': 'koji-target',
        }
        if scratch:
            additional_params['scratch'] = True
        if worker:
            additional_params['compose_ids'] = [42]
            params, build_json = self.get_worker_build_request(osbs, additional_params)
        else:
            params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'prebuild_plugins',
                                'resolve_module_compose', 'args')

        odcs_url = "https://odcs.example.com/odcs/1"
        odcs_insecure = False
        pdc_url = "https://pdc.example.com/rest_api/v1"
        pdc_insecure = False

        match_args = {
            "odcs_url": odcs_url,
            "odcs_insecure": odcs_insecure,
            "pdc_url": pdc_url,
            "pdc_insecure": pdc_insecure
        }
        args.pop('module_stream', None)
        args.pop('module_name', None)

        if worker:
            match_args['compose_ids'] = [42]

        assert match_args == args

        args = plugin_value_get(plugins, 'prebuild_plugins',
                                'flatpak_create_dockerfile', 'args')

        match_args = {
            "base_image": '{{BASE_IMAGE}}'
        }
        assert match_args == args

        if worker:
            plugin = get_plugin(plugins, "prebuild_plugins", "koji")
            assert plugin

            args = plugin['args']
            assert args['target'] == "koji-target"

        if not worker:
            args = plugin_value_get(plugins, 'buildstep_plugins',
                                    PLUGIN_BUILD_ORCHESTRATE_KEY, 'args')
            build_kwargs = args['build_kwargs']
            assert build_kwargs['flatpak'] is True

            config_kwargs = args['config_kwargs']
            assert config_kwargs['odcs_url'] == odcs_url
            assert config_kwargs['odcs_insecure'] == str(odcs_insecure)
            assert config_kwargs['pdc_url'] == pdc_url
            assert config_kwargs['pdc_insecure'] == str(pdc_insecure)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "postbuild_plugins", "import_image")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "exit_plugins", "import_image")

    @pytest.mark.parametrize('worker', [True, False])  # noqa:F811
    def test_not_flatpak(self, osbs, worker):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        if worker:
            params, build_json = self.get_worker_build_request(osbs, additional_params)
        else:
            params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "resolve_module_compose")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prebuild_plugins", "flatpak_create_dockerfile")
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, "prepublish_plugins", "flatpak_create_oci")


class TestArrangementV5(TestArrangementV4):
    """
    Orchestrator build differences from arrangement version 4:
    - resolve_composes enabled

    No worker build differences from arrangement version 4
    """

    ARRANGEMENT_VERSION = 5

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'reactor_config',
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                PLUGIN_KOJI_PARENT_KEY,
                'check_and_set_rebuild',
                PLUGIN_RESOLVE_COMPOSES_KEY,
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
                PLUGIN_FETCH_WORKER_METADATA_KEY,
                PLUGIN_COMPARE_COMPONENTS_KEY,
                'tag_from_config',
                PLUGIN_GROUP_MANIFESTS_KEY,
                PLUGIN_PULP_TAG_KEY,
                PLUGIN_PULP_SYNC_KEY,
            ],

            'exit_plugins': [
                PLUGIN_PULP_PUBLISH_KEY,
                PLUGIN_PULP_PULL_KEY,
                'import_image',
                'delete_from_registry',
                PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                'koji_tag_build',
                'store_metadata_in_osv3',
                'sendmail',
                'remove_built_image',
                PLUGIN_REMOVE_WORKER_METADATA_KEY,
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
                'flatpak_create_oci',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                'compress',
                PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
            ],

            'exit_plugins': [
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },
    }

    def test_resolve_composes(self, osbs):  # noqa:F811
        koji_target = 'koji-target'

        # These are hard coded by osbs fixture
        koji_hub = 'http://koji.example.com/kojihub'
        odcs_url = 'https://odcs.example.com/odcs/1'

        additional_params = {
            'base_image': 'fedora:latest',
            'target': koji_target,
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'prebuild_plugins', PLUGIN_RESOLVE_COMPOSES_KEY, 'args')

        assert args == {
            'koji_hub': koji_hub,
            'koji_target': koji_target,
            'odcs_url': odcs_url,
            'odcs_insecure': False,
        }

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    def test_import_image_renders(self, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "exit_plugins", "import_image")
            return

        args = plugin_value_get(plugins, 'exit_plugins',
                                'import_image', 'args')

        match_args = {
            "imagestream": "fedora23-something",
            "docker_image_repo": "registry.example.com/fedora23/something",
            "url": "/",
            "verify_ssl": False,
            "build_json_dir": "inputs",
            "use_auth": False
        }
        assert match_args == args


class TestArrangementV6(ArrangementBase):
    """
    No change to parameters, but use UserParams, BuildRequestV2, and PluginsConfiguration
    instead of Spec and BuildRequest. Most plugin arguments are not populated by
    osbs-client but are pulled from the REACTOR_CONFIG environment variable in
    atomic-reactor at runtime.

    Inherit from ArrangementBase, not the previous arrangements, because argument handling is
    different now and all previous tests break.

    No orchestrator build differences from arrangement version 5

    No worker build differences from arrangement version 5
    """

    ARRANGEMENT_VERSION = 6

    # Override common params
    COMMON_PARAMS = {
        'git_uri': TEST_GIT_URI,
        'git_ref': TEST_GIT_REF,
        'git_branch': TEST_GIT_BRANCH,
        'user': 'john-foo',
        'build_image': 'test',
        'base_image': 'test',
        'name_label': 'test',
    }

    ORCHESTRATOR_ADD_PARAMS = {
        'build_type': 'orchestrator',
        'platforms': ['x86_64'],
    }

    WORKER_ADD_PARAMS = {
        'build_type': 'worker',
        'platform': 'x86_64',
        'release': 1,
    }

    DEFAULT_PLUGINS = {
        # Changing this? Add test methods
        ORCHESTRATOR_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'reactor_config',
                'check_and_set_rebuild',
                PLUGIN_CHECK_AND_SET_PLATFORMS_KEY,
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'bump_release',
                'add_labels_in_dockerfile',
                PLUGIN_KOJI_PARENT_KEY,
                PLUGIN_RESOLVE_COMPOSES_KEY,
            ],

            'buildstep_plugins': [
                PLUGIN_BUILD_ORCHESTRATE_KEY,
            ],

            'prepublish_plugins': [
            ],

            'postbuild_plugins': [
                PLUGIN_FETCH_WORKER_METADATA_KEY,
                PLUGIN_COMPARE_COMPONENTS_KEY,
                'tag_from_config',
                PLUGIN_GROUP_MANIFESTS_KEY,
                PLUGIN_PULP_TAG_KEY,
                PLUGIN_PULP_SYNC_KEY,
            ],

            'exit_plugins': [
                PLUGIN_PULP_PUBLISH_KEY,
                PLUGIN_PULP_PULL_KEY,
                PLUGIN_VERIFY_MEDIA_KEY,
                'import_image',
                PLUGIN_KOJI_IMPORT_PLUGIN_KEY,
                'koji_tag_build',
                'store_metadata_in_osv3',
                'sendmail',
                'remove_built_image',
                PLUGIN_REMOVE_WORKER_METADATA_KEY,
            ],
        },

        # Changing this? Add test methods
        WORKER_INNER_TEMPLATE: {
            'prebuild_plugins': [
                'reactor_config',
                'resolve_module_compose',
                'flatpak_create_dockerfile',
                PLUGIN_ADD_FILESYSTEM_KEY,
                'inject_parent_image',
                'pull_base_image',
                'add_labels_in_dockerfile',
                'change_from_in_dockerfile',
                'hide_files',
                'add_help',
                'add_dockerfile',
                'distgit_fetch_artefacts',
                'fetch_maven_artifacts',
                'koji',
                'add_yum_repo_by_url',
                'inject_yum_repo',
                'distribution_scope',
            ],

            'buildstep_plugins': [
            ],

            'prepublish_plugins': [
                'squash',
                'flatpak_create_oci',
            ],

            'postbuild_plugins': [
                'all_rpm_packages',
                'tag_from_config',
                'tag_and_push',
                PLUGIN_PULP_PUSH_KEY,
                PLUGIN_EXPORT_OPERATOR_MANIFESTS_KEY,
                'compress',
                PLUGIN_KOJI_UPLOAD_PLUGIN_KEY,
            ],

            'exit_plugins': [
                'store_metadata_in_osv3',
                'remove_built_image',
            ],
        },
    }

    # override
    def get_plugins_from_buildrequest(self, build_request, template):
        kwargs = {
            'git_uri': TEST_GIT_URI,
            'git_ref': TEST_GIT_REF,
            'git_branch': TEST_GIT_BRANCH,
            'user': 'john-foo',
            'build_type': template.split('_')[0],
            'build_image': 'test',
            'base_image': 'test',
            'name_label': 'test',
        }
        build_request.set_params(**kwargs)
        return PluginsConfiguration(build_request.user_params).pt.template

    def get_build_request(self, build_type, osbs,  # noqa:F811
                          additional_params=None):
        params, build_json = super(TestArrangementV6, self).get_build_request(build_type, osbs,
                                                                              additional_params)
        # Make the REACTOR_CONFIG return look like previous returns
        env = build_json['spec']['strategy']['customStrategy']['env']
        for entry in env:
            if entry['name'] == 'USER_PARAMS':
                user_params = entry['value']
                break

        plugins_json = osbs.render_plugins_configuration(user_params)
        for entry in env:
            if entry['name'] == 'ATOMIC_REACTOR_PLUGINS':
                entry['value'] = plugins_json
                break
        else:
            env.append({
                'name': 'ATOMIC_REACTOR_PLUGINS',
                'value': plugins_json
            })

        return params, build_json

    def test_is_default(self):
        """
        Test this is the default arrangement
        """

        # Note! If this test fails it probably means you need to
        # derive a new TestArrangementV[n] class from this class and
        # move the method to the new class.
        assert DEFAULT_ARRANGEMENT_VERSION == self.ARRANGEMENT_VERSION

    @pytest.mark.parametrize('build_type', [  # noqa:F811
        'orchestrator',
        'worker',
    ])
    @pytest.mark.parametrize('scratch', [False, True])
    @pytest.mark.parametrize('base_image', ['koji/image-build', 'foo'])
    def test_pull_base_image(self, osbs, build_type, scratch, base_image):
        phase = 'prebuild_plugins'
        plugin = 'pull_base_image'
        additional_params = {
            'base_image': base_image,
        }
        if scratch:
            additional_params['scratch'] = True

        params, build_json = self.get_build_request(build_type, osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        assert get_plugin(plugins, phase, plugin)

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    @pytest.mark.parametrize('base_image', ['koji/image-build', 'foo'])
    def test_add_filesystem_in_worker(self, osbs, base_image, scratch):
        additional_params = {
            'base_image': base_image,
            'yum_repourls': ['https://example.com/my.repo'],
        }
        if scratch:
            additional_params['scratch'] = True
        params, build_json = self.get_worker_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'prebuild_plugins', PLUGIN_ADD_FILESYSTEM_KEY, 'args')

        assert 'repos' in args.keys()
        assert args['repos'] == params['yum_repourls']

    def test_resolve_composes(self, osbs):  # noqa:F811
        koji_target = 'koji-target'

        additional_params = {
            'base_image': 'fedora:latest',
            'target': koji_target,
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        assert get_plugin(plugins, 'prebuild_plugins', 'reactor_config')
        assert get_plugin(plugins, 'prebuild_plugins', PLUGIN_RESOLVE_COMPOSES_KEY)
        with pytest.raises(NoSuchPluginException):
            get_plugin(plugins, 'prebuild_plugins', 'resolve_module_compose')

    @pytest.mark.parametrize('scratch', [False, True])  # noqa:F811
    def test_import_image_renders(self, osbs, scratch):
        additional_params = {
            'base_image': 'fedora:latest',
        }
        if scratch:
            additional_params['scratch'] = True
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        if scratch:
            with pytest.raises(NoSuchPluginException):
                get_plugin(plugins, "exit_plugins", "import_image")
            return

        args = plugin_value_get(plugins, 'exit_plugins',
                                'import_image', 'args')

        match_args = {
            "imagestream": "fedora23-something",
        }
        assert match_args == args

    def test_orchestrate_render_no_platforms(self, osbs):  # noqa:F811
        additional_params = {
            'platforms': None,
            'base_image': 'fedora:latest',
        }
        _, build_json = self.get_orchestrator_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)

        args = plugin_value_get(plugins, 'buildstep_plugins',
                                PLUGIN_BUILD_ORCHESTRATE_KEY, 'args')

        assert 'platforms' not in args

    @pytest.mark.parametrize('extract_platform', ['x86_64', None])  # noqa:F811
    def test_export_operator_manifests(self, osbs, extract_platform):
        additional_params = {'base_image': 'fedora:latest'}
        match_args = {'platform': 'x86_64'}
        if extract_platform:
            additional_params['operator_manifests_extract_platform'] = extract_platform
            match_args['operator_manifests_extract_platform'] = extract_platform

        _, build_json = self.get_worker_build_request(osbs, additional_params)
        plugins = get_plugins_from_build_json(build_json)
        args = plugin_value_get(plugins, 'postbuild_plugins', 'export_operator_manifests', 'args')
        assert match_args == args
