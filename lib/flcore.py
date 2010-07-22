# Firelet - Distributed firewall management.
# Copyright (C) 2010 Federico Ceratto
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import csv

from hashlib import sha512
from collections import defaultdict
from git import InvalidGitRepositoryError, NoSuchPathError
from netaddr import IPAddress, IPNetwork
from os import unlink
from socket import inet_ntoa, inet_aton
from struct import pack, unpack
import logging

from flssh import SSHConnector
from flutils import Alert, Bunch

log = logging.getLogger()

# Logging levels:
#
# critical - application failing - red mark on webapp logging pane
# error - anything that prevents ruleset deployment - red mark
# warning - non-blocking errors - orange mark
# info - default messages - displayed on webapp
# debug - usually not logged and not displayed on webapp


try:
    import json
except ImportError:
    import simplejson as json

try:
    from itertools import product
except ImportError:
    def product(*args, **kwds):
        """List cartesian product - not available in Python 2.5"""
        pools = map(tuple, args) * kwds.get('repeat', 1)
        result = [[]]
        for pool in pools:
            result = [x+[y] for x in result for y in pool]
        for prod in result:
            yield tuple(prod)


protocols = ['IP','TCP', 'UDP', 'OSPF', 'IS-IS', 'SCTP', 'AH', 'ESP']


#input validation

def validc(c):
    n = ord(c)
    if 31< n < 127  and n not in (34, 39, 60, 62, 96):
        return True
    return False

def clean(s):
    """Remove dangerous characters.
    >>> clean(' !"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_')
    ' !#$%&()*+,-./0123456789:;=?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\]^_'
    """
    o = ''
    for x in s:
        n = ord(x)
        if 31< n < 127  and n not in (34, 39, 60, 62, 96):
            o += x
    return o

# Network objects

class NetworkObj(object):
    """Can be a host, a network or a hostgroup.
    It is a dictionary that exposes its keys as instance attributes.
    """
    def __repr__(self):
        return repr(self.__dict__)
    def __len__(self):
        return len(self.__dict__)


class Sys(NetworkObj):
    def __init__(self, name, ifaces={}):
        self.ifaces = ifaces


class Host(NetworkObj):
    def __init__(self, r):
        self.hostname=r[0]
        self.iface=r[1]
        self.ip_addr=r[2]
        self.masklen=r[3]
        self.local_fw=r[4]
        self.network_fw=r[5]
        self.mng=r[6]
        self.routed=r[7]

    def ipt(self):
        """String representation for iptables"""
        return "%s" % self.ip_addr

    def __contains__(self, other):
        """A host is "contained" in another host only when they have the same address"""""
        if isinstance(other, Host):
            return other.ip_addr == self.ip_addr

        elif isinstance(other, Network):
            addr_ok = net_addr(other.ip_addr, self.masklen) == self.ip_addr
            net_ok = other.masklen >= self.masklen
            return addr_ok and net_ok

    def mynetwork(self):
        """Return unnamed network directly connected to the host"""
        return Network(['', self.ip_addr, self.masklen])

class Network(NetworkObj):
    def __init__(self, r):
        self.name = r[0]
        self.update(r[1], r[2])

    def ipt(self):
        """String representation for iptables"""
        return "%s/%s" % (self.ip_addr, self.masklen)

    def update(self, addr, masklen):
        """Get the correct network address and update attributes"""
        masklen = int(masklen)
        real_addr = net_addr(addr, masklen)
        self.ip_addr = real_addr
        self.masklen = masklen
        return real_addr, masklen, real_addr == addr

    def __contains__(self, other):
        """Check if a host or a network falls inside this network"""
        if isinstance(other, Host):
            return net_addr(other.ip_addr, self.masklen) == self.ip_addr

        elif isinstance(other, Network):
            addr_ok = net_addr(other.ip_addr, self.masklen) == self.ip_addr
            net_ok = other.masklen >= self.masklen
            return addr_ok and net_ok


class HostGroup(NetworkObj):
    """A Host Group contains hosts, networks, and other host groups"""

    def __init__(self, li):
        self.name = li[0]
        if len(li) == 1:
            childs = []
        else:
            childs = li[1:]
        self.childs = childs

    def _flatten(self, i):
        """Flatten the host groups hierarchy and returns a list of strings"""
        if hasattr(i, 'childs'):  # "i" is a hostgroup _object_!
            childs = i.childs
            leaves =  sum(map(self._flatten, childs), [])
            for x in leaves:
                assert isinstance(x, str)
            return leaves
        if i in self._hbn:  # if "i" is a host group, fetch its childs:
            childs = self._hbn[i]
            leaves =  sum(map(self._flatten, childs), [])
            for x in leaves:
                assert isinstance(x, str)
            return leaves
        else: # it's a host or a network name (string)
            return [i]

    def flat(self, host_by_name, net_by_name, hg_by_name):
        """Flatten the host groups hyerarchy and returns Host or Network instances"""
        self._hbn = hg_by_name
        li = self._flatten(self)
        del(self._hbn)
        def res(o):
            assert isinstance(o, str), repr(o)
            if o in host_by_name:
                return host_by_name[o]
            elif o in net_by_name:
                return net_by_name[o]
            else:
                raise Exception,  "%s is not in %s or %s" % (o, repr(host_by_name), repr(net_by_name))

        return map(res, li)

##    def networks(self):
##        """Flatten the hostgroup and return its networks"""
##        return [n for n in self._flatten(self) if isinstance(n, Network)]
##
##    def hosts(self):
##        """Flatten the hostgroup and return its hosts"""
##        return filter(lambda i: type(i) == Host, self._flatten(self)) # better?
##        return [n for n in self._flatten(self) if isinstance(n, Host)]


class NetworkObjTable(object):
    """Contains a set of hosts or networks or hostgroups.
    They are stored as self._objdict where the key is the object name
    """
    def __init__(self):
        raise NotImplementedError

    def __str__(self):
        """Pretty-print as a table"""
        cols = zip(*self)
        cols_sizes = [(max(map(len,i))) for i in cols] # get the widest entry for each column

        def j((n, li)):
            return "%d  " % n + "  ".join((item.ljust(pad) for item, pad in zip(li, cols_sizes) ))
        return '\n'.join(map(j, enumerate(self)))

    def len(self):
        return len(self._objdict())

    def get(self, id=None, name=None):
        if name:
            return self._objdict[name]
        if id:
            return self._objdict.values()[id]



#files handling

class Table(list):
    """A list with pretty-print methods"""
    def __str__(self):
        cols = zip(*self)
        cols_sizes = [(max(map(len,i))) for i in cols] # get the widest entry for each column

        def j((n, li)):
            return "%d  " % n + "  ".join((item.ljust(pad) for item, pad in zip(li, cols_sizes) ))
        return '\n'.join(map(j, enumerate(self)))

    def len(self):
        return len(self)

def readcsv(n, d):
    f = open("%s/%s.csv" % (d, n))
    li = [x for x in f if not x.startswith('#') and x != '\n']
    r = csv.reader(li, delimiter=' ')
    f.close()
    return r

class SmartTable(object):
    """A list of Bunch instances. Each subclass is responsible to load and save files."""
    def __init__(self, d):
        self._dir = d
    def __repr__(self):
        return repr(self._list)
    def __iter__(self):
        return self._list.__iter__()
    def __len__(self):
        return len(self._list)
    def __getitem__(self, i):
        return self._list.__getitem__(i)
    def pop(self, i):
        x = self._list[i]
        del self._list[i]
        return x


class Rules(SmartTable):
    """A list of Bunch instances"""
    def __init__(self, d):
        self._dir = d
        li = readcsv('rules', d)
        self._list = [ Bunch(enabled=r[0], name=r[1], src=r[2], src_serv=r[3], dst=r[4],
                             dst_serv=r[5], action=r[6], log_level=r[7], desc=r[8]) for r in li ]

    def save(self):
        li = [[x.enabled, x.name, x.src, x.src_serv, x.dst, x.dst_serv,
                    x.action, x.log_level, x.desc] for x in self._list]
        savecsv('rules', li, self._dir)

    def moveup(self, rid):
        try:
            rules = self._list
            rules[rid], rules[rid - 1] = rules[rid - 1], rules[rid]
            self._list = rules
            self.save()
        except Exception, e:
            log.debug("Error in rules.moveup: %s" % e)
            #            say("Cannot move rule %d up." % rid)

    def movedown(self, rid):
        try:
            rules = self._list
            rules[rid], rules[rid + 1] = rules[rid + 1], rules[rid]
            self._list = rules[:]
            self.save()
        except Exception, e:
            #            say("Cannot move rule %d down." % rid)
            pass

    def rule_disable(self, rid):
        self._list[rid][0] = 'n'
        self.save()

    def rule_enable(self, rid):
        self._list[rid][0] = 'y'
        self.save()


class Hosts(SmartTable):
    """A list of Bunch instances"""
    def __init__(self, d):
        self._dir = d
        li = readcsv('hosts', d)
        self._list = []
        for r in li:
            q = r[0:7] + [r[7:]]
            b = Host(q)
            self._list.append(b)
    def save(self):
        """Flatten the routed network list and save"""
        li = [[x.hostname, x.iface, x.ip_addr, x.masklen, x.local_fw, x.network_fw, x.mng] + x.routed for x in self._list]
        savecsv('hosts', li, self._dir)


class HostGroups(SmartTable):
    """A list of Bunch instances"""
    def __init__(self, d):
        self._dir = d
        li = readcsv('hostgroups', d)
        self._list = [ HostGroup(r) for r in li ]
    def save(self):
        li = [[x.name] + x.childs for x in self._list]
        savecsv('hostgroups', li, self._dir)


class Networks(SmartTable):
    """A list of Bunch instances"""
    def __init__(self, d):
        self._dir = d
        li = readcsv('networks', d)
        self._list = [ Network(r) for r in li ]
    def save(self):
        li = [[x.name, x.ip_addr, x.masklen] for x in self._list]
        savecsv('networks', li, self._dir)


class Services(SmartTable):
    """A list of Bunch instances"""
    def __init__(self, d):
        self._dir = d
        li = readcsv('services', d)
        self._list = [ Bunch(name=r[0], proto=r[1], ports=r[2]) for r in li ]
    def save(self):
        li = [[x.name, x.proto, x.ports] for x in self._list]
        savecsv('services', li, self._dir)


# CSV files

def loadcsv(n, d):
    try:
        f = open("%s/%s.csv" % (d, n))
        li = [x for x in f if not x.startswith('#') and x != '\n']
#        if li[0] != '# Format 0.1 - Do not edit this line':
#            raise Exception, "Data format not supported in %s/%s.csv" % (d, n)
        r = Table(csv.reader(li, delimiter=' '))
        f.close()
        return r
    except IOError:
        return [] #FIXME: why?

def savecsv(n, stuff, d):
    log.debug('Writing "%s" in "%s"...' % (n, d))
    f = open("%s/%s.csv" % (d, n))
    comments = [x for x in f if x.startswith('#')]
    f.close()
    f = open("%s/%s.csv" % (d, n), 'wb')
    f.writelines(comments)
    writer = csv.writer(f,  delimiter=' ')
    writer.writerows(stuff)
    f.close()

def load_hosts_csv(n, d):
    """Read the hosts csv file, group the routed networks as a list"""
    li = loadcsv(n, d)

    mu = [[x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7:]] for x in li]
    for x in mu:
        assert len(x) == 8, "Wrong lenght '%s'" % repr(x)
        assert isinstance(x[7], list), 'Wrong %s'% repr(x)
        if x[7]:
            assert isinstance(x[7][0], str), 'Wrong %s'% repr(x)
    return mu

#def save_hosts_csv(n, mu, d):
#    """Save hosts on a csv file, flattening the input list."""
#    li = []
#    for x in mu:
#        if len(x) == 7:
#            o = x[0:6]
#        elif len(x) == 8 and len(x[7]) == 0:
#            o = x[0:7]
#        elif len(x) == 8 and isinstance(x[7], str):
#            o = x[0:7]
#            raise Exception, "Got str not list"
#        elif len(x) == 8 and isinstance(x[7][0], list):
#            o = x[0:7]
#            raise Exception, "Got list in list"
#        elif len(x) == 8:
#
#            log.debug(repr(x[7][0]))
#            o = x[0:7]+x[7]
#        else:
#            raise Exception, "Wrong list format"
#        li.append(o)
#        assert '[' not in repr(o)[1:-1], "Wrong csv conversion: %s" % repr(o)[1:-1]
#    savecsv(n, li, d)

# JSON files

def loadjson(n, d):
    f = open("%s/%s.json" % (d, n))
    s = f.read()
    f.close()
    return json.loads(s)


def savejson(n, obj, d):
    s = json.dumps(obj)
    f = open("%s/%s.json" % (d, n), 'wb')
    f.write(s)
    f.close()


# IP address parsing

def net_addr(a, n):
    q = IPNetwork('%s/%d' % (a, n)).network
    return str(q)

    addr = map(int, a.split('.'))
    x =unpack('!L',inet_aton(a))[0]  &  2L**(n + 1) -1
    return inet_ntoa(pack('L',x))




#class Hosts(NetworkObjTable):
#    def __init__(self, li):
#        self._li = li
#        pass
#    def aslist(self):
#        return self._li

class FireSet(object):
    """A container for the network objects.
    Upon instancing the objects are loaded.
    """
    def __init__(self, repodir):
        raise NotImplementedError

    # FireSet management methods
    # They are redefined in each FireSet subclass

    def save_needed(self):
        raise NotImplementedError

    def save(self):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def rollback(self, n):
        raise NotImplementedError

    def version_list(self):
        raise NotImplementedError

    # editing methods

    def fetch(self, table, rid):
        assert table in ('rules', 'hosts', 'hostgroups', 'services', 'network') ,  "Incorrect table name."
        try:
            return self.__dict__[table][rid]
        except Exception, e:
            Alert,  "Unable to fetch item %d in table %s: %s" % (rid, table, e)


    def delete(self, table, rid):
        assert table in ('rules', 'hosts', 'hostgroups', 'services', 'network') ,  "Incorrect table name."
        try:
            self.__dict__[table].pop(rid)
        except Exception, e:
            Alert,  "Unable to delete item %d in table %s: %s" % (rid, table, e)



    # deployment-related methods

    #
    # 1) The hostgroups are flattened, then the firewall rules are compiled into a big list of iptables commands.
    # 2) Firelet connects to the firewalls and fetch the iptables status and the existing interfaces (name, ip_addr, netmask)
    # 3) Based on this, the list is split in many sets - one for each firewall.

    #TODO: save the new configuration for each host and provide versioning.
    # Before deployment, compare the old (versioned), current (on the host) and new configuration for each firewall.
    # If current != versioned warn the user: someone made local changes.
    # Provide a diff of current VS new to the user before deploying.

    def _flattenhg(self, items, addr, net, hgs):
        """Flatten host groups tree, used in compile()"""
        def flatten1(item):
            li = addr.get(item), net.get(item), self._flattenhg(hgs.get(item), addr, net, hgs)
#            log.debug("Flattening... %s" % repr(item))
            return filter(None, li)[0]
        if not items: return None
        return map(flatten1, items)

    def _get_confs(self, keep_sessions=False):
        self._remote_confs = None
        d = {}      # {hostname: [management ip address list ], ... }    If the list is empty we cannot reach that host.
        for h in self.hosts:
            if h.hostname not in d: d[n] = []
            if int(h.mng):                            # IP address flagged for management
                d[n].append(addr)
        for n, x in d.iteritems():
            assert len(x), "No management IP address for %s " % n
        sx = SSHConnector(d, username='root')
        self._remote_confs = sx.get_confs()
        assert isinstance(self._remote_confs[0],  Bunch)
        if keep_sessions:
            return sx
        sx._disconnect()
        del(sx)

    def _check_ifaces(self):
        """Ensure that the interfaces configured on the hosts match the contents of the host table"""
        log.debug("Checking interfaces...")
        confs = self._remote_confs
        assert isinstance(confs, dict)
        for q in confs.values():
            assert isinstance(q, Bunch), repr(confs)
            assert len(q) == 2
        log.debug("Confs: %s" % repr(confs) )
        for h in self.hosts:
            if not h.hostname in confs:
                raise Alert, "Host %s not available." % h.hostname
            ip_a_s = confs[h.hostname].ip_a_s
            if not h.iface in ip_a_s:         #TODO: test this in unit testing
                raise Alert, "Interface %s missing on host %s" % (iface, hostname)
            ip_addr_v4, ip_addr_v6 = ip_a_s[h.iface]
            if h.ip_addr not in (ip_addr_v6,  ip_addr_v4.split('/')[0] ):
                raise Alert,"Wrong address on %s on interface %s: %s and %s(should be %s)" % (h.hostname, iface, ip_addr_v4, ip_addr_v6, h.ip_addr)

        #TODO: warn if there are extra interfaces?


    def _forwarded(self, remote, routed_nets, local_addr, local_masklen):
        """Tell if a remote net or ipaddr has to be routed through the local host.
        All params are strings"""
        if not remote: return True
        remote_IPN = IPNetwork(remote)
        if remote_IPN in IPNetwork(local_addr + '/' + local_masklen) and \
            remote_IPN != IPNetwork(local_addr + '/32'):  # that is input or output traffic, not forw.
                return True  # remote is in a directly conn. network
        else:
            ns = [ IPNetwork(y+'/'+w) for y, w in routed_nets]
            if sum((remote_IPN in _ for _ in ns)):
                return True
        return False

    def _oo_forwarded(self, src, dst, me, routed_nets, other_ifaces):
        """Tell if a src network or ipaddr has to be routed through the local host.
        All params are Host or Network instances"""
        if not src: return True
        if me.ip_addr == src.ip_addr: # this is input or output traffic, not to be forwarded
            return False
        if src in me.mynetwork(): # src is in a directly conn. network
            if dst in me.mynetwork(): # but dst is in the same network
                return False
            return True
        for rnet in routed_nets:
            if src in rnet and dst not in rnet:
                return True
        return False

    def compile_dict(self, hosts=None, rset=None):
        """Generate set of rules specific for each host.
            rd = {hostname: {iface: [rules, ] }, ... }
        """
        assert not self.save_needed(), "Configuration must be saved before deployment."
        if not hosts: hosts = self.hosts
        if not rset: rset = self.compile()
        # r[hostname][interface] = [rule, rule, ... ]
        rd = defaultdict(dict)

        for hostname,iface, ipa, masklen, locfw, netfw, mng, routed in hosts:  #FIXME: using SmartTable now
            myrules = [ r for r in rset if ipa in r ]   #TODO: match subnets as well
            if not iface in rd[hostname]: rd[hostname][iface] = []
            rd[hostname][iface].extend(myrules)
        log.debug("Rules compiled as dict: %s" % repr(rd))
        return rd


    def compile_rules(self):
        """Compile iptables rules to be deployed in a dict:
        { 'firewall_name': [rules...], ... }

        During the compilation many checks are performed."""

        assert not self.save_needed(), "Configuration must be saved before deployment."

        for rule in self.rules:
            assert rule.enabled in ('1', '0'), 'First field must be "1" or "0" in %s' % repr(rule)
        log.debug('Building dictionaries...')
        # build dictionaries to perform resolution
        addr = dict(((h.hostname + ":" + h.iface), h.ip_addr) for h in self.hosts) # host to ip_addr
        net = dict((n.name, (n.ip_addr, n.masklen)) for n in self.networks) # network name to addressing
#        hgs = dict((entry[0], (entry[1:])) for entry in self.hostgroups) # host groups
#        hg_flat = dict((hg, self._flattenhg(hgs[hg], addr, net, hgs)) for hg in hgs) # flattened to {hg: hosts and networks}

        proto_port = dict((sr.name, (sr.proto, sr.ports)) for sr in self.services) # protocol
        proto_port['*'] = (None, '') # special case for "any"      # port format: "2:4,5:10,10:33,40,50"

        host_by_name_col_iface = dict((h.hostname + ":" + h.iface, h) for h in self.hosts)
        host_by_name = dict((h.hostname, h) for h in self.hosts)
        net_by_name = dict((n.name, n) for n in self.networks)

        hg_by_name = dict((hg.name, hg.childs) for hg in self.hostgroups)  # hg name  to its child names

#        flat_hg_by_name = dict((hg.name, self._oo_flatten(hg, host_by_name_col_iface, )) for hg in hgs) #TODO finish this

        flat_hg = dict((hg.name, hg.flat(host_by_name_col_iface, net_by_name, hg_by_name)) for hg in self.hostgroups)

        def res(n):
            """Resolve flattened hostgroups, hosts and networks by name and provides None for '*' """
            if n in host_by_name_col_iface:
                return [host_by_name_col_iface[n]]
            elif n in net_by_name:
                return [net_by_name[n]]
            elif n in flat_hg:
                return flat_hg[n]
            elif n == '*':
                return [None]
            else:
                raise Alert, "Item %s is not defined." % n + repr(host_by_name_col_iface)

        log.debug('Compiling ruleset...')
        # for each rule, for each (src,dst) tuple, compiles a list  [ (proto, src, sports, dst, dports, log_val, rule_name, action), ... ]
        compiled = []
#        for ena, name, src, src_serv, dst, dst_serv, action, log_val, desc in self.rules:  # for each rule
        for rule in self.rules:  # for each rule
            if rule.enabled == '0':
                continue
            assert rule.action in ('ACCEPT', 'DROP'),  'The Action field must be "ACCEPT" or "DROP" in rule "%s"' % rule.name
            srcs = res(rule.src)
            dsts = res(rule.dst)    # list of Host and Network instances



            sproto, sports = proto_port[rule.src_serv]
            dproto, dports = proto_port[rule.dst_serv]
            assert sproto in protocols + [None], "Unknown source protocol: %s" % sproto
            assert dproto in protocols + [None], "Unknown dest protocol: %s" % dproto

            if sproto and dproto and sproto != dproto:
                raise Alert, "Source and destination protocol must be the same in rule \"%s\"." % rule.name
            if dproto:
                proto = " -p %s" % dproto.lower()
            elif sproto:
                proto = " -p %s" % sproto.lower()
            else:
                proto = ''

            if sports:
                ms = ' -m multiport' if ',' in sports else ''
                sports = "%s --sport %s" % (ms, sports)
            if dports:
                md = ' -m multiport' if ',' in dports else ''
                dports = "%s --dport %s" % (md, dports)

            for x in rule.name:
                assert validc(x), "Invalid character in '%s': x" % (repr(rule.name), repr(x))

            try:
                log_val = int(rule.log_level)
            except ValueError:
                raise Alert, "The logging field in rule '%s' is '%s' and must be an integer." % (rule.name, rule.log_level)

            for src, dst in product(srcs, dsts):
                assert isinstance(src, Host) or isinstance(src, Network) or src == None, repr(src)
                assert isinstance(dst, Host) or isinstance(dst, Network) or dst == None, repr(dst)
                compiled.append((proto, src, sports, dst, dports, log_val,  rule.name, rule.action))



        log.debug('Splicing ruleset...')
        # r[hostname] = [rule, rule, ... ]
        rd = defaultdict(list)
        rd = {}
        for proto, src, sports, dst, dports, log_val, name, action in compiled: # for each compiled rule
            _src = " -s %s" % src.ipt() if src else ''
            _dst = " -d %s" % dst.ipt() if dst else ''
            for h in self.hosts:   # for each host (interface)
                # Insert first rules
                if h.hostname not in rd:
                    rd[h.hostname] = {}
                    rd[h.hostname]['INPUT'] = ["-m state --state RELATED,ESTABLISHED -j ACCEPT"]
                    rd[h.hostname]['OUTPUT'] = ["-m state --state RELATED,ESTABLISHED -j ACCEPT"]
                    if h.network_fw == '1':
                        rd[h.hostname]['FORWARD'] = ["-m state --state RELATED,ESTABLISHED -j ACCEPT"]
                    else:
                        rd[h.hostname]['FORWARD'] = ["-j DROP"]
#                    rd[h.hostname].append("-A INPUT -s lo -j ACCEPT")
#                    rd[h.hostname].append("-A OUTPUT -d lo -j ACCEPT")

                if src and dst and src.ipt() == dst.ipt():
                    continue

                # Build INPUT rules: where the host is in the destination
                if dst and h in dst or not dst:
                    if log_val:
                        rd[h.hostname]['INPUT'].append("-i %s %s%s%s%s%s -j LOG --log-level %d --log-prefix %s" %   (h.iface, proto,  _src, sports, _dst, dports, log_val, name))
                    rd[h.hostname]['INPUT'].append("%s%s%s%s%s -j %s" %   (proto, _src, sports, _dst, dports, action))

                # Build OUTPUT rules: where the host is in the source
                if src and h in src or not src:
                    if log_val:
                        rd[h.hostname]['OUTPUT'].append("%s%s%s%s%s -j LOG --log-level %d --log-prefix %s" %   (proto, _src, sports, _dst, dports, log_val, name))
                    rd[h.hostname]['OUTPUT'].append("%s%s%s%s%s -j %s" %   (proto, _src, sports, _dst, dports, action))

                # Build FORWARD rules: where the source and destination are both in directly connected or routed networks
                if h.network_fw in ('0', 0, False):
                    continue
                resolved_routed = [net[r] for r in h.routed] # resolved routed nets [[addr,masklen],[addr,masklen]...]
                nets = [ IPNetwork("%s/%s" %(y, w)) for y, w in resolved_routed ]

                other_ifaces = [k for k in self.hosts if k.hostname == h.hostname and k.iface != h.iface]


                forw = self._oo_forwarded(src, dst, h, resolved_routed, other_ifaces)

                if forw:
                    rd[h.hostname]['FORWARD'].append("%s%s%s%s%s -j LOG --log-level %d --log-prefix %s" %   (proto, _src, sports, _dst, dports, log_val, name))
                    rd[h.hostname]['FORWARD'].append("%s%s%s%s%s -j %s" %   (proto, _src, sports, _dst, dports, action))

            # "for every host"
        # "for every rule"

        log.debug("Rules compiled")
        for k, v in rd.iteritems():
            log.debug("------------------------- %s -------------------------" % k)
            for chain, li in v.iteritems():
                for x in li:
                    log.debug("%s %s" % (chain, x))
        return rd


    def compile(self, hosts=None, rset=None):
        return self.compile_rules()

    def _diff_table(self):      # Ridefined below!
        """Generate an HTML table containing the changes between the existing and the compiled iptables ruleset *on each host* """
        #TODO: this is a hack - rewrite it with two-step comparison (existing VS old, existing VS new) and rule injection.
        from difflib import HtmlDiff
        differ = HtmlDiff()
        html = ''
        for hostname, (ex_iptables_d, ex_ip_a_s) in self._remote_confs.iteritems():   # existing iptables ruleset
            # rd[hostname][iface] = [rule, rule,]
            if hostname in self.rd:
                ex_iptables = ex_iptables_d['filter']
                a = self.rd[hostname]
                new_iptables = sum(a.values(),[]) # flattened
                diff = differ.make_table(ex_iptables, new_iptables,context=True,numlines=0)
                html += "<h4 class='dtt'>%s</h4>" % hostname + diff
            else:
                log.debug('%s removed?' % hostname) #TODO: review this, manage *new* hosts as well
        return html

    def _diff_table(self):
        """Generate an HTML table containing the changes between the existing and the compiled iptables ruleset *on each host* """
        #TODO: this is a hack - rewrite it with two-step comparison (existing VS old, existing VS new) and rule injection.
        html = ''
        for hostname, (ex_iptables_d, ex_ip_a_s) in self._remote_confs.iteritems():   # existing iptables ruleset
            # rd[hostname][iface] = [rule, rule,]
            if hostname in self.rd:
                ex_iptables = ex_iptables_d['filter']
                a = self.rd[hostname]
                new_iptables = sum(a.values(),[]) # flattened
                new_s = set(new_iptables)
                ex_s = set(ex_iptables)
                tab = ''
                for r in list(new_s - ex_s):
                    tab += '<tr class="add"><td>%s</td></tr>' % r
                for r in list(ex_s - new_s):
                    tab += '<tr class="del"><td>%s</td></tr>' % r
                if tab:
                    html += "<h4 class='dtt'>%s</h4><table class='phdiff_table'>%s</table>" % (hostname, tab)
            else:
                log.debug('%s removed?' % hostname) #TODO: review this, manage *new* hosts as well
        if not html:
            return '<p>The firewalls are up to date. No deployment needed.</p>'
        return html

        # unused
        from difflib import HtmlDiff
        differ = HtmlDiff()
        html = ''
        for hostname, (ex_iptables_d, ex_ip_a_s) in self._remote_confs.iteritems():   # existing iptables ruleset
            # rd[hostname][iface] = [rule, rule,]
            if hostname in self.rd:
                ex_iptables = ex_iptables_d['filter']
                a = self.rd[hostname]
                new_iptables = sum(a.values(),[]) # flattened
                diff = differ.make_table(ex_iptables, new_iptables,context=True,numlines=0)
                html += "<h4 class='dtt'>%s</h4>" % hostname + diff
            else:
                log.debug('%s removed?' % hostname) #TODO: review this, manage *new* hosts as well
        return html

    def check(self):
        """Check the configuration on the firewalls.
        """
        if self.save_needed():
            raise Alert, "Configuration must be saved before check."

        comp_rules = self.compile()
        sx = self._get_confs(keep_sessions=True)
        self._check_ifaces()
        self.rd = self.compile_dict()
        log.debug('Diff table: %s' % self._diff_table())

        return self._diff_table()


    def deploy(self, ignore_unreachables=False, replace_ruleset=False):
        """Check and then deploy the configuration to the firewalls.
        Some ignore flags can be set to force the deployment even in case of errors.
        """
        assert not self.save_needed(), "Configuration must be saved before deployment."

        comp_rules = self.compile()
        sx = self._get_confs(keep_sessions=True)
        self._check_ifaces()
        self.rd = self.compile_dict()
        sx.deliver_confs(self.rd)
        sx.apply_remote_confs()


    def _check_remote_confs(self):
        pass




class GitFireSet(FireSet):
    """FireSet implementing Git to manage the configuration repository"""
    def __init__(self, repodir='/var/lib/firelet'):
        self.rules = Rules(repodir)
        self.hosts = Hosts(repodir)
        self.hostgroups = HostGroups(repodir)
        self.services = Services(repodir)
        self.networks = Networks(repodir)
        self._git_repodir = repodir
        if 'fatal: Not a git repository' in self._git('status')[1]:
            log.debug('Creating Git repo...')
            self._git('init .')
            self._git('add *')
            self._git('commit -m "First commit."')

    def version_list(self):
        """Parse git log --date=iso and returns a list of lists:
        [ [author, date, [msg lines], commit_id ], ... ]
        """
        o, e = self._git('log --date=iso')
        if e:
            Alert, e   #TODO
        li = []
        msg = []
        author = None
        for r in o.split('\n'):
            if r.startswith('commit '):
                if author:
                    li.append([author, date, msg, commit])
                commit = r[7:]
                msg = []
            elif r.startswith('Author: '):
                author = r[8:]
            elif r.startswith('Date: '):
                date = r[8:]
            elif r:
                msg.append(r.strip())
        return li

    def version_diff(self, commit_id):
        """Parse git diff <commit_id>"""
        o, e = self._git('diff %s' % commit_id)
        if e:
            Alert, e   #TODO
        li = []
        for x in o.split('\n')[2:]: #TODO: Looks fragile
            if x.startswith('+++'):
                li.append((x[6:-4], 'title'))
            elif x.startswith('---') or x.startswith('@@'):
                pass
            elif x.startswith('-'):
                li.append((x[1:], 'del'))
            elif x.startswith('+'):
                li.append((x[1:], 'add'))
            else:
                li.append((x, ''))
        return li

    def _git(self, cmd):
        from subprocess import Popen, PIPE
        log.debug('Executing "/usr/bin/git %s" in "%s"' % (cmd, self._git_repodir))
        p = Popen('/usr/bin/git %s' % cmd, shell=True, cwd=self._git_repodir, stdout=PIPE, stderr=PIPE)
        p.wait()
        return p.communicate()

    def save(self, msg):
        """Commit changes if needed."""
        if not msg:
            msg = '(no message)'
        self._git("add *")
        self._git("commit -m '%s'" % msg)

    def reset(self):
        """Reset Git to last commit."""
        o, e = self._git('reset --hard')
        assert 'HEAD is now at' in o, "Git reset --hard output: '%s' error: '%s'" % (o, e)

    def rollback(self, n):
        """Rollback to n commits ago"""
        try:
            n = int(n)
        except ValueError:
            raise Alert, "rollback requires an integer"
        self.reset()
        o, e = self._git("reset --hard HEAD~%d" % n)
        assert 'HEAD is now at' in o, "Git reset --hard HEAD~%d output: '%s' error: '%s'" % (n, o, e)

    def save_needed(self):
        """True if commit is required: files has been changed"""
        o, e = self._git('status')
        log.debug("Git status output: '%s' error: '%s'" % (o, e))
        if 'nothing to commit ' in o:
            return False
        elif '# On branch master' in o:
            return True
        else:
            raise Alert, "Git status output: '%s' error: '%s'" % (o, e)

    # GitFireSet editing

    def _write(self, table):
        """Write the changes to disk without committing on Git"""
        if table == 'hosts':
            self.hosts.save()
        elif table == 'networks':
            self.networks.save()
        elif table == 'services':
            self.services.save()
        elif table == 'rules':
            self.rules.save()
        else:
            savecsv(table, self.__dict__[table], d=self._git_repodir)

    def delete(self, table, rid):
        assert table in ('rules', 'hosts', 'hostgroups', 'services', 'networks') ,  "Wrong table name for deletion: %s" % table
        try:
            self.__dict__[table].pop(rid)
        except IndexError, e:
            raise Alert, "The element n. %d is not present in table '%s'" % (rid, table)
        self._write(table)




class DemoGitFireSet(GitFireSet):
    """Based on GitFireSet. Provide a demo version without real network interaction.
    The status of the simulated remote hosts is kept in memory.
    """
    def __init__(self):
        GitFireSet.__init__(self)
        self._demo_rulelist = defaultdict(list)

    def _get_confs(self, keep_sessions=False):
        def ip_a_s(n):
            """Build a dict: {'eth0': (addr, None)} for a given host"""
            i = ((iface, (addr, None)) for hn,  iface, ipa, masklen, locfw, netfw, mng, routed in self.hosts if hn == n   )
            return dict(i)
        d = {} # {hostname: Bunch() }
        for n, iface, addr, masklen, locfw, netfw, mng, routed in self.hosts:
            d[n] = Bunch(filter=self._demo_rulelist[n], ip_a_s=ip_a_s(n))
        self._remote_confs = d

    def deploy(self, ignore_unreachables=False, replace_ruleset=False):
        """Check and then deploy the configuration to the simulated firewalls."""
        if self.save_needed():
            raise Alert, "Configuration must be saved before deployment."
        comp_rules = self.compile()
        sx = self._get_confs(keep_sessions=True)
        self._check_ifaces()
        self.rd = self.compile_dict() # r[hostname][interface] = [rule, rule, ... ]
        for n, k in self.rd.iteritems():
            rules = sum(k.values(),[])
            self._demo_rulelist[n] = rules



# #  User management  # #

#TODO: add creation and last access date?

class Users(object):
    """User management, with password hashing.
    users = {'username': ['role','pwdhash','email'], ... }
    """

    def __init__(self, d):
        self._dir = d
        try:
            self._users = loadjson('users', d=d)
        except:
            self._users = {} #TODO: raise alert?

    def _save(self):
        savejson('users', self._users, d=self._dir)

    def _hash(self, u, pwd): #TODO: should I add salting?
        return sha512("%s:::%s" % (u, pwd)).hexdigest()

    def create(self, username, role, pwd, email=None):
        assert username, "Username must be provided."
        assert username not in self._users, "User already exists."
        self._users[username] = [role, self._hash(username, pwd), email]
        self._save()

    def update(self, username, role=None, pwd=None, email=None):
        assert username in self._users, "Non existing user."
        if role is not None:
            self._users[username][0] = role
        if pwd is not None:
            self._users[username][1] = self._hash(username, pwd)
        if email is not None:
            self._users[username][2] = email
        self._save()

    def delete(self, username):
        try:
            self._users.pop(username)
        except KeyError:
            raise Alert, "Non existing user."
        self._save()

    def validate(self, username, pwd):
        assert username, "Missing username."
        assert username in self._users, "Incorrect user or password."
        assert self._hash(username, pwd) == self._users[username][1], "Incorrect user or password."














