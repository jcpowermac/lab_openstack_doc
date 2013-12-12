#Sources:
# http://effbot.org/zone/element-xpath.htm
# http://eli.thegreenplace.net/2012/03/15/processing-xml-in-python-with-elementtree/
# http://wiki.libvirt.org/page/SSHSetup
# http://docs.python.org/2/tutorial/datastructures.html#dictionaries
# http://libvirt.org/git/?p=libvirt.git;a=blob;f=tools/virsh-domain-monitor.c
# http://www.linuxproblem.org/art_9.html
# http://j2labs.tumblr.com/post/4477180133/ssh-with-pythons-paramiko

__author__ = 'jcallen'
import os
import libvirt
import sys
import prettytable
import novaclient.v1_1.client as nvclient
from neutronclient.v2_0 import client
import keystoneclient.v2_0.client as ksclient
import xml.etree.ElementTree as ET
import paramiko
import re
import argparse


def keystone_connect(hostname, username, password, tenant):
    auth_url = "http://%s:35357/v2.0" % hostname

    keystone = ksclient.Client(auth_url=auth_url,
                           username=username,
                           password=password,
                           tenant_name=tenant)
    if keystone is None:
        print 'Failed to open connection to OpenStack instance'
        sys.exit(1)

    return keystone


def neutron_connect(hostname, keystone):
    os_url = "http://%s:9696/" % hostname
    neutron_conn = client.Client(endpoint_url=os_url, token=keystone.auth_token)
    if neutron_conn is None:
        print 'Failed to open connection to OpenStack instance'
        sys.exit(1)

    return neutron_conn


def nova_connect(hostname, username, password, tenant):
    authurl = "http://%s:5000/v2.0" % hostname
    nova_conn = nvclient.Client(username, password, tenant, authurl, service_type="compute")
    if nova_conn is None:
        print 'Failed to open connection to OpenStack instance'
        sys.exit(1)

    return nova_conn


def qemu_connect(hostname, username):
    qemu = "qemu+ssh://%s@%s/system" % (username, hostname)
    qemu_conn = libvirt.openReadOnly(qemu)
    if qemu_conn is None:
        print 'Failed to open connection libvirtd'
        sys.exit(1)

    return qemu_conn


def ssh_connect(host, username, private_key, port=22):
    """Helper function to initiate an ssh connection to a host."""
    transport = paramiko.Transport((host, port))

    if os.path.exists(private_key):
        rsa_key = paramiko.RSAKey.from_private_key_file(private_key)
        transport.connect(username=username, pkey=rsa_key)
    else:
        raise TypeError("Incorrect private key path")

    return transport


def exec_cmd(transport, command):
    """Executes a command on the same server as the provided
    transport
    """
    try:
        channel = transport.open_session()
        channel.exec_command(command)
        if channel.recv_exit_status() == 0:
            output = channel.makefile('rb', -1).readlines()
            return output
        else:
            stderr_output = channel.makefile_stderr('rb', -1).readlines()
            print stderr_output
            print "error: " + channel.recv_exit_status()
            return ""
    except:
        print sys.exc_info()


def show_brctl_veth(device, transport):
    BRCTL = "/usr/sbin/brctl"
    command = BRCTL + " show " + device
    output = exec_cmd(transport, command)
    if output != "":
        ifline = output[1]
        match = re.findall("(qvb[\w-]+$)", ifline)
        veth = match[0]
        return veth


def ethtool_adapter_stats(device, transport):
    ETHTOOL = "/usr/sbin/ethtool"
    command = ETHTOOL + " -S " + device
    output = exec_cmd(transport, command)
    if output != "":
        ifline = output[1]
        match = re.findall("peer_ifindex: ([\d]+)$", ifline)
        peer_ifindex = match[0]
        return peer_ifindex


def ip_link(peer_ifindex, transport):
    ETHTOOL = "/usr/sbin/ip"
    command = ETHTOOL + " link "
    output = exec_cmd(transport, command)
    if output != "":
        for interfaces in output:
            ifline = interfaces
            matchstring = "^%s:\s([\w-]+)" % peer_ifindex
            match = re.findall(matchstring, ifline)
            if len(match) != 0:
                veth_pair2 = match[0]
                return veth_pair2


def ovs_ofctl(action, device, transport):
    """
    Executes ovs-ofctl via paramiko ssh client

    Requires a sudoers entry:
    Defaults !requiretty
    user ALL = NOPASSWD: /usr/bin/ovs-ofctl
    """
    OFCTL="sudo /usr/bin/ovs-ofctl"
    command = OFCTL + " " + action + " " + device
    output = exec_cmd(transport, command)
    return output


def ovs_ofctl_dump_flows(device, tag, transport):
    output = ovs_ofctl("dump-flows", device, transport)
    if output != "":
        for line in output:
            matchstring = "mod_vlan_vid:%s" % tag
            if re.search(matchstring, line) is not None:
                matchstring = "dl_vlan=(\d+)"
                match = re.findall(matchstring, line)
                if len(match) != 0:
                    #print match
                    vlan = match[0]
                    return vlan


def ovs_vsctl(action, device, transport):
    """
    Executes ovs-vsctl via paramiko ssh client

    Requires a sudoers entry:
    Defaults !requiretty
    user ALL = NOPASSWD: /usr/bin/ovs-vsctl
    """

    VSCTL="sudo /usr/bin/ovs-vsctl"
    command = VSCTL + " " + action + " " + device
    output = exec_cmd(transport, command)
    return output


def ovs_vsctl_list_port(device, transport):
    output = ovs_vsctl("list port", device, transport)
    if output != "":
        for line in output:
            matchstring = "^tag[\s:]*(\d+)"
            match = re.findall(matchstring, line)
            if len(match) != 0:
                tag = match[0]
                return tag


def ovs_vsctl_port_to_br(device, transport):
    output = ovs_vsctl("port-to-br", device, transport)
    bridge = output[0].split("\n")[0]
    return bridge


def get_router_id(neutron_conn, net_id):
    """
    Yes, I know there is a bug here, will resolve later!
    """
    net = neutron_conn.show_network(net_id)

    for subnet in net['network']['subnets']:
        ports = neutron_conn.list_ports(retrieve_all=True, subnet_id=subnet)
        for port in ports['ports']:
            if port['device_owner'] == 'network:router_interface':
                return port['device_id']


def domiflist(domxml):
    tree = ET.ElementTree(ET.fromstring(domxml))
    elements = tree.iterfind("./devices/interface")

    for ele in elements:
        interface_type = ele.attrib['type']
        if interface_type == "bridge":
            bridge = ele.find("source[@bridge]").attrib["bridge"]
            tap = ele.find("target[@dev]").attrib["dev"]
            mac = ele.find("mac[@address]").attrib["address"]
            return bridge, tap, mac
        else:
            print "Error: Currently will only work with bridged interfaces."


def args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hostname', help='Hostname of OpenStack instance', required=True)
    parser.add_argument('--os-username', help='OpenStack Username', required=True)
    parser.add_argument('--os-password', help='OpenStack Password', required=True)
    parser.add_argument('--os-tenant-name', help='OpenStack Tenant', required=True)
    parser.add_argument('--username', help='Username of OpenStack host', required=True)
    parser.add_argument('--priv-key', help='Location of private SSH Key', required=True)
    arguments = parser.parse_args()
    return arguments


def main():

    args_namespace = args()

    nova_conn = nova_connect(args_namespace.hostname,
                             args_namespace.os_username,
                             args_namespace.os_password,
                             args_namespace.os_tenant_name)
    qemu_conn = qemu_connect(args_namespace.hostname, args_namespace.username)
    keystone_conn = keystone_connect(args_namespace.hostname,
                                     args_namespace.os_username,
                                     args_namespace.os_password,
                                     args_namespace.os_tenant_name)
    neutron_conn = neutron_connect(args_namespace.hostname, keystone_conn)

    try:
        pt = prettytable.PrettyTable(["Property", "Value"])
        pt.align = "l"
        servers = nova_conn.servers.list(detailed=True)
        for srv in servers:
            interface_list = srv.interface_list()
            for interface in interface_list:
                router_id = get_router_id(neutron_conn, interface.net_id)
                print ("\nTroubleshooting commands:")
                print ("ip netns exec qrouter-%s ip a" % router_id)
                print ("ip netns exec qrouter-%s ip r" % router_id)
                print ("ip netns exec qrouter-%s iptables -t nat -L -nv" % router_id)
                print ("ip netns exec qrouter-%s iptables -S -t nat" % router_id)
                print ("ip netns exec qrouter-%s ping 8.8.8.8" % router_id)

            dom = qemu_conn.lookupByUUIDString(srv.id)  # get domain from UUID
            xml = dom.XMLDesc()
            bridge, tap, mac = domiflist(xml)

            transport = ssh_connect(args_namespace.hostname, args_namespace.username, args_namespace.priv_key)
            veth_pair1 = show_brctl_veth(bridge, transport)
            peer_ifindex = ethtool_adapter_stats(veth_pair1, transport)
            veth_pair2 = ip_link(peer_ifindex, transport)
            ovsbridge = ovs_vsctl_port_to_br(veth_pair2, transport)
            tag = ovs_vsctl_list_port(veth_pair2, transport)
            vlan = ovs_ofctl_dump_flows(ovsbridge, tag, transport)

            pt.add_row(["OpenStack Name", srv.human_id])
            pt.add_row(["QEMU Name", dom.name()])
            pt.add_row(["UUID", dom.UUIDString()])
            pt.add_row(["tap", tap])
            pt.add_row(["linuxbridge", bridge])
            pt.add_row(["MAC Address", mac])
            pt.add_row(["VETH Pair #1", veth_pair1])
            pt.add_row(["VETH Pair #2", veth_pair2])
            pt.add_row(["ovsbridge", ovsbridge])
            pt.add_row(["TAG", tag])
            pt.add_row(["VLAN", vlan])

            print(pt)
            pt.clear_rows()

    except:
        print sys.exc_info()
        sys.exit(1)

if __name__ == '__main__':
    main()
