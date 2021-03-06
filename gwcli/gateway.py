#!/usr/bin/env python

import json

from gwcli.node import UIGroup, UINode, UIRoot

from gwcli.hostgroup import HostGroups
from gwcli.storage import Disks
from gwcli.client import Clients, CHAP
from gwcli.utils import (this_host,
                         GatewayAPIError, GatewayError,
                         APIRequest,
                         console_message, get_port_state,
                         valid_iqn)

import ceph_iscsi_config.settings as settings

import rtslib_fb.root as root

from gwcli.ceph import CephGroup

# FIXME - code is using a self signed cert common across all gateways
# the embedded urllib3 package will issue warnings when ssl cert validation is
# disabled - so this disable_warnings stops the user interface from being
# bombed
from requests.packages import urllib3

__author__ = 'Paul Cuzner'

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ISCSIRoot(UIRoot):

    def __init__(self, shell, endpoint=None):
        UIRoot.__init__(self, shell)

        self.error = False
        self.error_msg = ''
        self.interactive = True           # default interactive mode

        if settings.config.api_secure:
            self.http_mode = 'https'
        else:
            self.http_mode = 'http'

        if endpoint is None:

            self.local_api = ('{}://127.0.0.1:{}/'
                              'api'.format(self.http_mode,
                                           settings.config.api_port))

        else:
            self.local_api = endpoint

        self.config = {}
        # Establish the root nodes within the UI, for the different components

        self.disks = Disks(self)
        self.ceph = CephGroup(self)
        self.target = ISCSITarget(self)

    def refresh(self):
        self.config = self._get_config()

        if not self.error:

            if 'disks' in self.config:
                self.disks.refresh(self.config['disks'])
            else:
                self.disks.refresh({})

            if 'gateways' in self.config:
                self.target.gateway_group = self.config['gateways']
            else:
                self.target.gateway_group = {}

            if 'clients' in self.config:
                self.target.client_group = self.config['clients']
            else:
                self.target.client_group = {}

            self.target.refresh()

            self.ceph.refresh()

        else:
            # Unable to get the config, tell the user and exit the cli
            self.logger.critical("Unable to access the configuration "
                                 "object : {}".format(self.error_msg))
            raise GatewayError


    def _get_config(self, endpoint=None):

        if not endpoint:
            endpoint = self.local_api

        api = APIRequest(endpoint + "/config")
        api.get()

        if api.response.status_code == 200:
            return api.response.json()
        else:
            self.error = True
            self.error_msg = "REST API failure, code : " \
                             "{}".format(api.response.status_code)
            return {}

    def export_ansible(self, config):

        this_gw = this_host()
        ansible_vars = []
        ansible_vars.append("seed_monitor: {}".format(self.ceph.local_ceph.healthy_mon))
        ansible_vars.append("cluster_name: {}".format(settings.config.cluster_name))
        ansible_vars.append("gateway_keyring: {}".format(settings.config.gateway_keyring))
        ansible_vars.append("deploy_settings: true")
        ansible_vars.append("perform_system_checks: true")
        ansible_vars.append('gateway_iqn: "{}"'.format(config['gateways']['iqn']))
        ansible_vars.append('gateway_ip_list: "{}"'.format(",".join(config['gateways']['ip_list'])))
        ansible_vars.append("# rbd device definitions")
        ansible_vars.append("rbd_devices:")

        disk_template = ("  - {{ pool: '{}', image: '{}', size: '{}', "
                         "host: '{}', state: 'present' }}")

        for disk in self.disks.children:

            ansible_vars.append(disk_template.format(disk.pool,
                                                     disk.image,
                                                     disk.size_h,
                                                     this_gw))
        ansible_vars.append("# client connections")
        ansible_vars.append("client_connections:")
        client_template = ("  - {{ client: '{}', image_list: '{}', "
                           "chap: '{}', status: 'present' }}")

        for client in sorted(config['clients'].keys()):
            client_metadata = config['clients'][client]
            lun_data = client_metadata['luns']
            sorted_luns = [s[0] for s in sorted(lun_data.iteritems(),
                                                key=lambda (x, y): y['lun_id'])]
            chap = CHAP(client_metadata['auth']['chap'])
            ansible_vars.append(client_template.format(client,
                                                       ','.join(sorted_luns),
                                                       chap.chap_str))
        for var in ansible_vars:
            print(var)

    def export_copy(self, config):

        fmtd_config = json.dumps(config, sort_keys=True,
                                 indent=4, separators=(',', ': '))
        print(fmtd_config)

    def ui_command_export(self, mode='ansible'):

        self.logger.debug("CMD: export mode={}".format(mode))

        current_config = self._get_config()

        if mode == "ansible":
            self.export_ansible(current_config)
        elif mode == 'copy':
            self.export_copy(current_config)

    def ui_command_info(self):
        self.logger.debug("CMD: info")

        if settings.config.trusted_ip_list:
            display_ips = ','.join(settings.config.trusted_ip_list)
        else:
            display_ips = 'None'

        console_message("HTTP mode          : {}".format(self.http_mode))
        console_message("Rest API port      : {}".format(settings.config.api_port))
        console_message("Local endpoint     : {}".format(self.local_api))
        console_message("Local Ceph Cluster : {}".format(settings.config.cluster_name))
        console_message("2ndary API IP's    : {}".format(display_ips))


class ISCSITarget(UIGroup):
    help_intro = '''
                 The iscsi-target group shows you...bla
                 '''

    def __init__(self, parent):
        UIGroup.__init__(self, 'iscsi-target', parent)
        self.gateway_group = {}
        self.client_group = {}


    def ui_command_create(self, target_iqn):
        """
        Create a gateway target. This target is defined across all gateway nodes,
        providing the client with a single 'image' for iscsi discovery.

        Only ONE target is supported, at this time.
        """

        self.logger.debug("CMD: /iscsi create {}".format(target_iqn))

        defined_targets = [tgt.name for tgt in self.children]
        if len(defined_targets) > 0:
            self.logger.error("Only ONE iscsi target image is supported")
            return

        # We need LIO to be empty, so check there aren't any targets defined
        local_lio = root.RTSRoot()
        current_target_names = [tgt.wwn for tgt in local_lio.targets]
        if current_target_names:
            self.logger.error("Local LIO instance already has LIO configured "
                              "with a target - unable to continue")
            return

        # OK - this request is valid, but is the IQN usable?
        if not valid_iqn(target_iqn):
            self.logger.error("IQN name '{}' is not valid for "
                              "iSCSI".format(target_iqn))
            return


        # 'safe' to continue with the definition
        self.logger.debug("Create an iscsi target definition in the UI")

        local_api = ('{}://127.0.0.1:{}/api/'
                     'target/{}'.format(self.http_mode,
                                        settings.config.api_port,
                                        target_iqn))

        api = APIRequest(local_api)
        api.put()

        if api.response.status_code == 200:
            self.logger.info('ok')
            # create the target entry in the UI tree
            Target(target_iqn, self)
        else:
            self.logger.error("Failed to create the target on the local node")

            raise GatewayAPIError("iSCSI target creation failed - "
                                  "{}".format(api.response.json()['message']))

    def ui_command_clearconfig(self, confirm=None):
        """
        The 'clearconfig' command allows you to return the configuration to an
        unused state: LIO on each gateway will be cleared, and gateway
        definitions in the configuration object will be removed.

        > clearconfig confirm=true

        In order to run the clearconfig command, all clients and disks *must*
        have already have been removed.
        """

        self.logger.debug("CMD: clearconfig confirm={}".format(confirm))

        confirm = self.ui_eval_param(confirm, 'bool', False)
        if not confirm:
            self.logger.error("To clear the configuration you must specify "
                              "confirm=true")
            return

        # get a new copy of the config dict over the local API
        # check that there aren't any disks or client listed
        local_api = ("{}://127.0.0.1:{}/api/"
                     "config".format(self.http_mode,
                                     settings.config.api_port))

        api = APIRequest(local_api)
        api.get()

        if api.response.status_code != 200:
            self.logger.error("Unable to get fresh copy of the configuration")
            raise GatewayAPIError

        current_config = api.response.json()
        num_clients = len(current_config['clients'].keys())
        num_disks = len(current_config['disks'].keys())

        if num_clients > 0 or num_disks > 0:
            self.logger.error("Clients({}) and Disks({}) must be removed first"
                              " before clearing the gateway "
                              "configuration".format(num_clients,
                                                     num_disks))
            return

        self.clear_config(current_config['gateways'])

    def clear_config(self, gateway_group):

        # we need to process the gateways, leaving the local machine until
        # last to ensure we don't fall foul of the api auth check
        gw_list = [gw_name for gw_name in gateway_group
                   if isinstance(gateway_group[gw_name], dict)]
        gw_list.remove(this_host())
        gw_list.append(this_host())

        for gw_name in gw_list:

            gw_api = ('{}://{}:{}/api/'
                      'gateway/{}'.format(self.http_mode,
                                          gw_name,
                                          settings.config.api_port,
                                          gw_name))

            api = APIRequest(gw_api)
            api.delete()
            if api.response.status_code != 200:
                msg = api.response.json()['message']
                self.logger.error("Delete of {} failed : {}".format(gw_name,
                                                                    msg))
                raise GatewayAPIError
            else:
                self.logger.debug("- deleted {}".format(gw_name))

        # gateways removed, so lets delete the objects from the UI tree
        self.reset()

        # remove any bookmarks stored in the prefs.bin file
        del self.shell.prefs['bookmarks']

        self.logger.info('ok')

    def refresh(self):

        self.logger.debug("Refreshing gateway & client information")
        self.reset()
        if 'iqn' in self.gateway_group:
            tgt = Target(self.gateway_group['iqn'], self)
            tgt.gateway_group.load(self.gateway_group)
            tgt.client_group.load(self.client_group)

    def summary(self):
        return "Targets: {}".format(len(self.children)), None

class Target(UIGroup):

    help_info = '''
                The iscsi target bla
                '''

    def __init__(self, target_iqn, parent):

        UIGroup.__init__(self, target_iqn, parent)
        self.target_iqn = target_iqn
        self.gateway_group = GatewayGroup(self)
        self.client_group = Clients(self)
        self.host_groups = HostGroups(self)

    def summary(self):
        return "Gateways: {}".format(len(self.gateway_group.children)), None


class GatewayGroup(UIGroup):

    help_intro = '''
                 The gateway-group shows you the high level details of the
                 iscsi gateway nodes that have been configured. It also allows
                 you to add further gateways to the configuration, but when
                 creating new gateways, it is your responsibility to ensure the
                 following requirements are met:
                 - device-mapper-mulitpath
                 - ceph_iscsi_config

                 In addition multipath.conf must be set up specifically for use
                 as a gateway.

                 If in doubt, use Ansible :)
                 '''

    def __init__(self,  parent):

        UIGroup.__init__(self, 'gateways', parent)

        # record the shortcut
        shortcut = self.shell.prefs['bookmarks'].get('gateways', None)
        if not shortcut or shortcut is not self.path:

            self.shell.prefs['bookmarks']['gateways'] = self.path
            self.shell.prefs.save()
            self.shell.log.debug("Bookmarked %s as %s."
                                 % (self.path, 'gateways'))

    def load(self, gateway_group):
        # define the host entries from the gateway_group dict
        gateway_list = [gw for gw in gateway_group
                        if isinstance(gateway_group[gw], dict)]
        for gateway_name in gateway_list:
            Gateway(self, gateway_name, gateway_group[gateway_name])

    def ui_command_info(self):

        self.logger.debug("CMD: ../gateways/ info")

        for child in self.children:
            console_message(child)

    def ui_command_refresh(self):
        """
        refresh allows you to refresh the connection status of each of the
        configured gateways (i.e. check the up/down state).
        """

        if len(self.children) > 0:
            self.logger.debug("{} gateways to refresh".format(len(self.children)))
            for gw in self.children:
                gw.refresh()
        else:
            self.logger.error("No gateways to refresh")

    def ui_command_create(self, gateway_name, ip_address, nosync=False):
        """
        Define a gateway to the gateway group for this iscsi target. The
        first host added should be the gateway running the command

        gateway_name ... should resolve to the hostname of the gateway
        ip_address ..... is the IP v4 address of the interface the iscsi
                         portal should use
        nosync ......... by default new gateways are sync'd with the
                         existing configuration by cli. By specifying nosync
                         the sync step is bypassed - so the new gateway
                         will need to have it's rbd-target-gw daemon
                         restarted to apply the current configuration
        """

        self.logger.debug("CMD: ../gateways/ create {} {} "
                          "nosync={}".format(gateway_name,
                                             ip_address,
                                             nosync))

        local_gw = this_host()
        current_gateways = [tgt.name for tgt in self.children]


        if gateway_name != local_gw and len(current_gateways) == 0:
            # the first gateway defined must be the local machine. By doing
            # this the initial create uses 127.0.0.1, and places it's portal IP
            # in the gateway ip list. Once the gateway ip list is defined, the
            # api server can resolve against the gateways - until the list is
            # defined only a request from 127.0.0.1 is acceptable to the api
            self.logger.error("The first gateway defined must be the local "
                              "machine")
            return

        if local_gw in current_gateways:
            current_gateways.remove(local_gw)

        config = self.parent.parent.parent._get_config()
        if not config:
            self.logger.error("Unable to refresh local config"
                              " over API - sync aborted, restart rbd-target-gw"
                              " on {} to sync".format(gateway_name))

        if nosync:
            sync_text = "sync skipped"
        else:
            sync_text = ("sync'ing {} disk(s) and "
                         "{} client(s)".format(len(config['disks']),
                                               len(config['clients'])))

        self.logger.info("Adding gateway, {}".format(sync_text))

        gw_api = '{}://{}:{}/api'.format(self.http_mode,
                                         "127.0.0.1",
                                         settings.config.api_port)
        gw_rqst = gw_api + '/gateway/{}'.format(gateway_name)
        gw_vars = {"nosync": nosync,
                   "ip_address": ip_address}

        api = APIRequest(gw_rqst, data=gw_vars)
        api.put()

        if api.response.status_code != 200:

            msg = api.response.json()['message']

            self.logger.error("Failed : {}".format(msg))
            return

        self.logger.debug("{}".format(api.response.json()['message']))
        self.logger.debug("Adding gw to UI")

        # Target created OK, get the details back from the gateway and
        # add to the UI. We have to use the new gateway to ensure what
        # we get back is current (the other gateways will lag until they see
        # epoch xattr change on the config object)
        new_gw_endpoint = ('{}://{}:{}/'
                           'api'.format(self.http_mode,
                                        gateway_name,
                                        settings.config.api_port))

        config = self.parent.parent.parent._get_config(endpoint=new_gw_endpoint)
        gw_config = config['gateways'][gateway_name]
        Gateway(self, gateway_name, gw_config)

        self.logger.info('ok')

    def summary(self):

        up_count = len([gw.state for gw in self.children if gw.state == 'UP'])
        gw_count = len(self.children)

        return ("Up: {}/{}, Portals: {}".format(up_count,
                                                gw_count,
                                                gw_count),
                up_count == gw_count)


    def _interactive_shell(self):
        return self.parent.parent.parent.interactive

    interactive = property(_interactive_shell,
                           doc="determine whether the cli is running in "
                               "interactive mode")


class Gateway(UINode):

    display_attributes = ["name",
                          "gateway_ip_list",
                          "portal_ip_address",
                          "inactive_portal_ips",
                          "active_luns",
                          "tpgs",
                          "service_state"]

    TCP_PORT = 3260

    def __init__(self, parent, gateway_name, gateway_config):
        """
        Create the LIO element
        :param parent: parent object the gateway group object
        :param gateway_config: dict holding the fields that define the gateway
        :return:
        """

        UINode.__init__(self, gateway_name, parent)

        for k, v in gateway_config.iteritems():
            self.__setattr__(k, v)

        self.state = "DOWN"
        self.service_state = {"iscsi": {"state": "DOWN",
                                        "port": Gateway.TCP_PORT},
                              "api": {"state": "DOWN",
                                      "port": settings.config.api_port}
                              }
        self.refresh()

    def refresh(self):

        self.logger.debug("- checking iSCSI/API ports on "
                          "{}".format(self.name))
        self._get_state()

        up_count = len([self.service_state[s]["state"]
                        for s in self.service_state
                        if self.service_state[s]["state"] == "UP"])

        if up_count == len(self.service_state):
            self.state = "UP"
        elif up_count == 0:
            self.state = "DOWN"
        else:
            self.state = "PARTIAL"

    def _get_state(self):
        """
        Determine iSCSI and gateway API service state
        :return:
        """

        for svc in self.service_state:

            result = get_port_state(self.portal_ip_address,
                                    self.service_state[svc]["port"])

            self.service_state[svc]["state"] = "UP" if result == 0 else "DOWN"

    def summary(self):

        state = self.state
        return "{} ({})".format(self.portal_ip_address,
                                state), (state == "UP")
