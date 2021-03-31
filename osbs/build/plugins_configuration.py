"""
Copyright (c) 2018, 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
from __future__ import print_function, absolute_import, unicode_literals

import abc
import logging
import os
import json

import six

from osbs.constants import BUILD_TYPE_ORCHESTRATOR
from osbs.exceptions import OsbsException

logger = logging.getLogger(__name__)


class PluginsTemplate(object):
    def __init__(self, build_json_dir, template_path, customize_conf_path=None):
        self._template = None
        self._customize_conf = None
        self._build_json_dir = build_json_dir
        self._template_path = template_path
        self._customize_conf_path = customize_conf_path

    @property
    def template(self):
        if self._template is None:
            path = os.path.join(self._build_json_dir, self._template_path)
            logger.debug("loading template from path %s", path)
            try:
                with open(path, "r") as fp:
                    self._template = json.load(fp)
            except (IOError, OSError) as ex:
                raise OsbsException("Can't open template '%s': %s" %
                                    (path, repr(ex)))
        return self._template

    @property
    def customize_conf(self):
        if self._customize_conf is None:
            if self._customize_conf_path is None:
                self._customize_conf = {}
            else:
                path = os.path.join(self._build_json_dir, self._customize_conf_path)
                logger.info('loading customize conf from path %s', path)
                try:
                    with open(path, "r") as fp:
                        self._customize_conf = json.load(fp)
                except IOError:
                    # File not found, which is perfectly fine. Set to empty dict
                    logger.info('failed to find customize conf from path %s', path)
                    self._customize_conf = {}
        return self._customize_conf

    def remove_plugin(self, phase, name, reason=None):
        """
        if config contains plugin, remove it
        """
        for p in self.template[phase]:
            if p.get('name') == name:
                self.template[phase].remove(p)
                if reason:
                    logger.info('Removing %s:%s, %s', phase, name, reason)
                break

    def add_plugin(self, phase, name, args, reason=None):
        """
        if config has plugin, override it, else add it
        """
        plugin_modified = False

        for plugin in self.template[phase]:
            if plugin['name'] == name:
                plugin['args'] = args
                plugin_modified = True

        if not plugin_modified:
            self.template[phase].append({"name": name, "args": args})
            if reason:
                logger.info('%s:%s with args %s, %s', phase, name, args, reason)

    def get_plugin_conf(self, phase, name):
        """
        Return the configuration for a plugin.

        Raises KeyError if there are no plugins of that type.
        Raises IndexError if the named plugin is not listed.
        """
        match = [x for x in self.template[phase] if x.get('name') == name]
        return match[0]

    def has_plugin_conf(self, phase, name):
        """
        Check whether a plugin is configured.
        """
        try:
            self.get_plugin_conf(phase, name)
            return True
        except (KeyError, IndexError):
            return False

    def _get_plugin_conf_or_fail(self, phase, name):
        try:
            conf = self.get_plugin_conf(phase, name)
        except KeyError:
            raise RuntimeError("Invalid template: plugin phase '%s' misses" % phase)
        except IndexError:
            raise RuntimeError("no such plugin in template: \"%s\"" % name)
        return conf

    def set_plugin_arg(self, phase, name, arg_key, arg_value):
        plugin_conf = self._get_plugin_conf_or_fail(phase, name)
        plugin_conf.setdefault("args", {})
        plugin_conf['args'][arg_key] = arg_value

    def set_plugin_arg_valid(self, phase, plugin, name, value):
        if value is not None:
            self.set_plugin_arg(phase, plugin, name, value)
            return True
        return False

    def to_json(self):
        return json.dumps(self.template)


@six.add_metaclass(abc.ABCMeta)
class PluginsConfigurationBase(object):
    """Abstract class for Plugins Configuration

    Following properties must be implemented:
      * pt_path - path to inner config

    Following methods must be implemented:
      * render - method generates plugin config JSON

    Class contains methods that configures plugins. These methods should be
    used only if needed in specific subclass implementation
    """
    def __init__(self, user_params):
        self.user_params = user_params

        customize_conf_path = (
            self.user_params.customize_conf
            if hasattr(self.user_params, 'customize_conf')
            else None
        )

        self.pt = PluginsTemplate(
            self.user_params.build_json_dir,
            self.pt_path,
            customize_conf_path,
        )

    @abc.abstractproperty
    def pt_path(self):
        """Property returns path to plugins template JSON file

        :return: file path
        """
        raise NotImplementedError

    @abc.abstractmethod
    def render(self):
        """Return plugins configuration JSON

        :rval: str
        :return: JSON
        """
        raise NotImplementedError

    def render_add_filesystem(self):
        phase = 'prebuild_plugins'
        plugin = 'add_filesystem'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'repos',
                                         self.user_params.yum_repourls)
            self.pt.set_plugin_arg_valid(phase, plugin, 'from_task_id',
                                         self.user_params.filesystem_koji_task_id)
            self.pt.set_plugin_arg_valid(phase, plugin, 'architecture',
                                         self.user_params.platform)
            self.pt.set_plugin_arg_valid(phase, plugin, 'koji_target',
                                         self.user_params.koji_target)

    def render_add_image_content_manifest(self):
        phase = 'prebuild_plugins'
        plugin = 'add_image_content_manifest'
        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'remote_sources',
                                         self.user_params.remote_sources)

    def render_add_labels_in_dockerfile(self):
        phase = 'prebuild_plugins'
        plugin = 'add_labels_in_dockerfile'
        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.release:
                release_label = {'release': self.user_params.release}
                self.pt.set_plugin_arg(phase, plugin, 'labels', release_label)

    def render_add_yum_repo_by_url(self):
        if self.pt.has_plugin_conf('prebuild_plugins', "add_yum_repo_by_url"):
            self.pt.set_plugin_arg_valid('prebuild_plugins', "add_yum_repo_by_url", "repourls",
                                         self.user_params.yum_repourls)

    def render_customizations(self):
        """
        Customize template for site user specified customizations
        """
        disable_plugins = self.pt.customize_conf.get('disable_plugins', [])
        if not disable_plugins:
            logger.debug('No site-user specified plugins to disable')
        else:
            for plugin in disable_plugins:
                try:
                    self.pt.remove_plugin(plugin['plugin_type'], plugin['plugin_name'],
                                          'disabled at user request')
                except KeyError:
                    # Malformed config
                    logger.info('Invalid custom configuration found for disable_plugins')

        enable_plugins = self.pt.customize_conf.get('enable_plugins', [])
        if not enable_plugins:
            logger.debug('No site-user specified plugins to enable')
        else:
            for plugin in enable_plugins:
                try:
                    msg = 'enabled at user request'
                    self.pt.add_plugin(plugin['plugin_type'], plugin['plugin_name'],
                                       plugin['plugin_args'], msg)
                except KeyError:
                    # Malformed config
                    logger.info('Invalid custom configuration found for enable_plugins')

    def render_check_user_settings(self):
        phase = 'prebuild_plugins'
        plugin = 'check_user_settings'
        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'flatpak',
                                         self.user_params.flatpak)

    def render_flatpak_update_dockerfile(self):
        phase = 'prebuild_plugins'
        plugin = 'flatpak_update_dockerfile'

        if self.pt.has_plugin_conf(phase, plugin):

            self.pt.set_plugin_arg_valid(phase, plugin, 'compose_ids',
                                         self.user_params.compose_ids)

    def render_koji(self):
        """
        if there is yum repo in user params, don't pick stuff from koji
        """
        phase = 'prebuild_plugins'
        plugin = 'koji'
        if self.pt.has_plugin_conf(phase, plugin):

            self.pt.set_plugin_arg_valid(phase, plugin, "target",
                                         self.user_params.koji_target)

    def render_bump_release(self):
        """
        If the bump_release plugin is present, configure it
        """
        phase = 'prebuild_plugins'
        plugin = 'bump_release'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        # For flatpak, we want a name-version-release of
        # <name>-<stream>-<module_build_version>.<n>, where the .<n> makes
        # sure that the build is unique in Koji
        if self.user_params.flatpak:
            self.pt.set_plugin_arg(phase, plugin, 'append', True)

    def render_check_and_set_platforms(self):
        """
        If the check_and_set_platforms plugin is present, configure it
        """
        phase = 'prebuild_plugins'
        plugin = 'check_and_set_platforms'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        if self.user_params.koji_target:
            self.pt.set_plugin_arg(phase, plugin, "koji_target",
                                   self.user_params.koji_target)

    def render_import_image(self, use_auth=None):
        """
        Configure the import_image plugin
        """
        # import_image is a multi-phase plugin
        if self.pt.has_plugin_conf('exit_plugins', 'import_image'):

            self.pt.set_plugin_arg('exit_plugins', 'import_image', 'imagestream',
                                   self.user_params.imagestream_name)

    def render_inject_parent_image(self):
        phase = 'prebuild_plugins'
        plugin = 'inject_parent_image'
        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, 'koji_parent_build',
                                         self.user_params.koji_parent_build)

    def render_koji_upload(self, use_auth=None):
        phase = 'postbuild_plugins'
        name = 'koji_upload'
        if not self.pt.has_plugin_conf(phase, name):
            return

        def set_arg(arg, value):
            self.pt.set_plugin_arg(phase, name, arg, value)

        set_arg('koji_upload_dir', self.user_params.koji_upload_dir)
        set_arg('platform', self.user_params.platform)
        set_arg('report_multiple_digests', True)

    def render_pin_operator_digest(self):
        phase = 'prebuild_plugins'
        name = 'pin_operator_digest'

        replacement_pullspecs = self.user_params.operator_bundle_replacement_pullspecs
        modifications_url = self.user_params.operator_csv_modifications_url

        if self.pt.has_plugin_conf(phase, name):
            if replacement_pullspecs:
                self.pt.set_plugin_arg(phase, name, 'replacement_pullspecs', replacement_pullspecs)

            if modifications_url:
                self.pt.set_plugin_arg(
                    phase, name, 'operator_csv_modifications_url', modifications_url
                )

    def render_export_operator_manifests(self):
        phase = 'postbuild_plugins'
        name = 'export_operator_manifests'
        if not self.pt.has_plugin_conf(phase, name):
            return

        self.pt.set_plugin_arg(phase, name, 'platform', self.user_params.platform)
        if self.user_params.operator_manifests_extract_platform:
            self.pt.set_plugin_arg(phase, name, 'operator_manifests_extract_platform',
                                   self.user_params.operator_manifests_extract_platform)

    def render_koji_tag_build(self):
        phase = 'exit_plugins'
        plugin = 'koji_tag_build'
        if self.pt.has_plugin_conf(phase, plugin):

            self.pt.set_plugin_arg_valid(phase, plugin, 'target',
                                         self.user_params.koji_target)

    def render_orchestrate_build(self):
        phase = 'buildstep_plugins'
        plugin = 'orchestrate_build'
        if not self.pt.has_plugin_conf(phase, plugin):
            return

        # Parameters to be used in call to create_worker_build
        worker_params = [
            'component', 'git_branch', 'git_ref', 'git_uri', 'koji_task_id',
            'filesystem_koji_task_id', 'scratch', 'koji_target', 'user', 'yum_repourls',
            'arrangement_version', 'koji_parent_build', 'isolated', 'reactor_config_map',
            'reactor_config_override', 'git_commit_depth',
        ]

        build_kwargs = self.user_params.to_dict(worker_params)
        # koji_target is passed as target for some reason
        build_kwargs['target'] = build_kwargs.pop('koji_target', None)

        if self.user_params.flatpak:
            build_kwargs['flatpak'] = True

        self.pt.set_plugin_arg_valid(phase, plugin, 'platforms', self.user_params.platforms)
        self.pt.set_plugin_arg(phase, plugin, 'build_kwargs', build_kwargs)

        config_kwargs = {}

        if not self.user_params.buildroot_is_imagestream:
            config_kwargs['build_from'] = 'image:' + self.user_params.build_image

        self.pt.set_plugin_arg(phase, plugin, 'config_kwargs', config_kwargs)

    def render_resolve_composes(self):
        phase = 'prebuild_plugins'
        plugin = 'resolve_composes'

        if not self.pt.has_plugin_conf(phase, plugin):
            return

        self.pt.set_plugin_arg_valid(phase, plugin, 'compose_ids',
                                     self.user_params.compose_ids)

        self.pt.set_plugin_arg_valid(phase, plugin, 'signing_intent',
                                     self.user_params.signing_intent)

        self.pt.set_plugin_arg_valid(phase, plugin, 'koji_target',
                                     self.user_params.koji_target)

        self.pt.set_plugin_arg_valid(phase, plugin, 'repourls',
                                     self.user_params.yum_repourls)

    def render_tag_from_config(self):
        """Configure tag_from_config plugin"""
        phase = 'postbuild_plugins'
        plugin = 'tag_from_config'

        if not self.pt.has_plugin_conf(phase, plugin):
            return

        unique_tag = self.user_params.image_tag.split(':')[-1]
        tag_suffixes = {'unique': [unique_tag], 'primary': [], 'floating': []}

        if self.user_params.build_type == BUILD_TYPE_ORCHESTRATOR:
            additional_tags = self.user_params.additional_tags or set()

            if self.user_params.scratch:
                pass
            elif self.user_params.isolated:
                tag_suffixes['primary'].extend(['{version}-{release}'])
            elif self.user_params.tags_from_yaml:
                tag_suffixes['primary'].extend(['{version}-{release}'])
                tag_suffixes['floating'].extend(additional_tags)
            else:
                tag_suffixes['primary'].extend(['{version}-{release}'])
                tag_suffixes['floating'].extend(['latest', '{version}'])
                tag_suffixes['floating'].extend(additional_tags)

        self.pt.set_plugin_arg(phase, plugin, 'tag_suffixes', tag_suffixes)

    def render_pull_base_image(self):
        """Configure pull_base_image"""
        phase = 'prebuild_plugins'
        plugin = 'pull_base_image'

        if self.user_params.parent_images_digests:
            self.pt.set_plugin_arg(phase, plugin, 'parent_images_digests',
                                   self.user_params.parent_images_digests)

    def render_koji_delegate(self):
        """Configure koji_delegate"""
        phase = 'prebuild_plugins'
        plugin = 'koji_delegate'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.triggered_after_koji_task:
                self.pt.set_plugin_arg(phase, plugin, 'triggered_after_koji_task',
                                       self.user_params.triggered_after_koji_task)

    def render_tag_and_push(self):
        """Configure tag_and_push plugin"""
        phase = 'postbuild_plugins'
        plugin = 'tag_and_push'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.koji_target:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_target',
                    self.user_params.koji_target
                )

    def render_fetch_sources(self):
        """Configure fetch_sources"""
        phase = 'prebuild_plugins'
        plugin = 'fetch_sources'

        if self.pt.has_plugin_conf(phase, plugin):
            if self.user_params.sources_for_koji_build_nvr:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_build_nvr',
                    self.user_params.sources_for_koji_build_nvr
                )

            if self.user_params.sources_for_koji_build_id:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'koji_build_id',
                    self.user_params.sources_for_koji_build_id
                )

            if self.user_params.signing_intent:
                self.pt.set_plugin_arg(
                    phase, plugin,
                    'signing_intent',
                    self.user_params.signing_intent
                )

    def render_download_remote_source(self):
        phase = 'prebuild_plugins'
        plugin = 'download_remote_source'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg(phase, plugin, 'remote_sources',
                                   self.user_params.remote_sources)

    def render_resolve_remote_source(self):
        phase = 'prebuild_plugins'
        plugin = 'resolve_remote_source'

        if self.pt.has_plugin_conf(phase, plugin):
            self.pt.set_plugin_arg_valid(phase, plugin, "dependency_replacements",
                                         self.user_params.dependency_replacements)


class PluginsConfiguration(PluginsConfigurationBase):
    """Plugin configuration for image builds"""

    @property
    def pt_path(self):
        arrangement_version = self.user_params.arrangement_version
        build_type = self.user_params.build_type
        #    <build_type>_inner:<arrangement_version>.json
        return '{}_inner:{}.json'.format(build_type, arrangement_version)

    def render(self):
        self.user_params.validate()
        # adjust for custom configuration first
        self.render_customizations()

        # Set parameters on each plugin as needed
        self.render_add_filesystem()
        self.render_add_labels_in_dockerfile()
        self.render_add_yum_repo_by_url()
        self.render_bump_release()
        self.render_check_and_set_platforms()
        self.render_check_user_settings()
        self.render_flatpak_update_dockerfile()
        self.render_import_image()
        self.render_inject_parent_image()
        self.render_koji()
        self.render_koji_tag_build()
        self.render_koji_upload()
        self.render_pin_operator_digest()
        self.render_export_operator_manifests()
        self.render_orchestrate_build()
        self.render_pull_base_image()
        self.render_resolve_composes()
        self.render_tag_from_config()
        self.render_koji_delegate()
        self.render_download_remote_source()
        self.render_resolve_remote_source()
        self.render_add_image_content_manifest()
        return self.pt.to_json()


class SourceContainerPluginsConfiguration(PluginsConfigurationBase):
    """Plugins configuration for source container image builds"""

    @property
    def pt_path(self):
        arrangement_version = self.user_params.arrangement_version
        # orchestrator_sources_inner:<arrangement_version>.json
        return 'orchestrator_sources_inner:{}.json'.format(arrangement_version)

    def render(self):
        self.user_params.validate()
        # adjust for custom configuration first
        self.render_customizations()

        # Set parameters on each plugin as needed
        # self.render_bump_release()  # not needed yet
        self.render_fetch_sources()
        self.render_koji()
        self.render_koji_tag_build()
        self.render_tag_and_push()

        return self.pt.to_json()
