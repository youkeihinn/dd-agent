# std
from collections import defaultdict

# project
from checks import AgentCheck

# 3rd party
from pysnmp.entity.rfc3413.oneliner import cmdgen
from pysnmp.smi.exval import noSuchInstance, noSuchObject
from pysnmp.smi import builder
import pysnmp.proto.rfc1902 as snmp_type

# Additional types that are not part of the SNMP protocol. cf RFC 2856
(CounterBasedGauge64, ZeroBasedCounter64) = builder.MibBuilder().importSymbols("HCNUM-TC","CounterBasedGauge64", "ZeroBasedCounter64")

# Metric type that we support
SNMP_COUNTERS = [snmp_type.Counter32.__name__, snmp_type.Counter64.__name__, ZeroBasedCounter64.__name__]
SNMP_GAUGES = [snmp_type.Gauge32.__name__, CounterBasedGauge64.__name__]

def reply_invalid(oid):
    return noSuchInstance.isSameTypeWith(oid) or \
           noSuchObject.isSameTypeWith(oid)

class SnmpCheck(AgentCheck):

    cmd_generator = None

    def __init__(self, name, init_config, agentConfig, instances=None):
        AgentCheck.__init__(self, name, init_config, agentConfig, instances)

        self.interface_list = {}

        # Load Custom MIB directory
        mibs_path = None
        if init_config is not None:
            mibs_path = init_config.get("mibs_folder")
        SnmpCheck.create_command_generator(mibs_path)

    @classmethod
    def create_command_generator(cls, mibs_path=None):
        '''
        Create a command generator to perform all the snmp query
        If mibs_path is not None, load the mibs present in the custom mibs
        folder (Need to be in pysnmp format)
        '''
        cls.cmd_generator = cmdgen.CommandGenerator()
        if mibs_path is not None:
            mib_builder = cls.cmd_generator.snmpEngine.msgAndPduDsp.\
                          mibInstrumController.mibBuilder
            mib_sources = mib_builder.getMibSources() + (
                    builder.DirMibSource(mibs_path),
                    )
            mib_builder.setMibSources(*mib_sources)

    @classmethod
    def get_auth_data(cls, instance):
        '''
        Generate a Security Parameters object based on the configuration of the instance
        See http://pysnmp.sourceforge.net/docs/current/security-configuration.html
        '''
        if "community_string" in instance:
            # SNMP v1 - SNMP v2
            return cmdgen.CommunityData(instance['community_string'])
        elif "user" in instance:
            # SNMP v3
            user = instance["user"]
            auth_key = None
            priv_key = None
            auth_protocol = None
            priv_protocol = None
            if "authKey" in instance:
                auth_key = instance["authKey"]
                auth_protocol = cmdgen.usmHMACMD5AuthProtocol
            if "privKey" in instance:
                priv_key = instance["privKey"]
                auth_protocol = cmdgen.usmHMACMD5AuthProtocol
                priv_protocol = cmdgen.usmDESPrivProtocol
            if "authProtocol" in instance:
                auth_protocol = getattr(cmdgen, instance["authProtocol"])
            if "privProtocol" in instance:
                priv_protocol = getattr(cmdgen, instance["privProtocol"])
            return cmdgen.UsmUserData(user, auth_key, priv_key, auth_protocol, priv_protocol)
        else:
            raise Exception("An authentication method needs to be provided")

    @classmethod
    def get_transport_target(cls, instance):
        '''
        Generate a Transport target object based on the configuration of the instance
        '''
        if "ip_address" not in instance:
            raise Exception("An IP address needs to be specified")
        ip_address = instance["ip_address"]
        port = instance.get("port", 161) # Default SNMP port
        return cmdgen.UdpTransportTarget((ip_address, port))

    def check_table(self, instance, oids):
        interface_list = {}
        transport_target = self.get_transport_target(instance)
        auth_data = self.get_auth_data(instance)

        snmp_command = self.cmd_generator.nextCmd
        error_indication, error_status, error_index, var_binds = snmp_command(
            auth_data,
            transport_target,
            *oids,
            lookupValues = True,
            lookupNames = True
            )

        results = defaultdict(dict)
        if error_indication:
            raise Exception("{0} for instance {1}".format(error_indication, instance["ip_address"]))
        else:
            if error_status:
                raise Exception("{0} for instance {1}".format(error_status.prettyPrint(), instance["ip_address"]))
            else:
                for table_row in var_binds:
                    for result_oid, value in table_row:
                        object = result_oid.getMibSymbol()
                        metric =  object[1]
                        indexes = object[2]
                        results[metric][indexes] = value

        return results

    def check(self, instance):
        tags = instance.get("tags",[])
        ip_address = instance["ip_address"]
        table_oids = []
        # Check the metrics completely defined
        for metric in instance.get('metrics', []):
            if 'MIB' in metric:
                try:
                    assert "table" in metric or "symbol" in metric
                    to_query = metric.get("table", metric.get("symbol"))
                    table_oids.append(cmdgen.MibVariable(metric["MIB"], to_query))
                except Exception as e:
                    self.log.warning("Can't generate MIB object for variable : %s\nException: %s", metric, e)
            else:
                raise Exception('Unsupported metrics format in config file')
        self.log.debug("Querying device %s for %s oids", ip_address, len(table_oids))
        results = self.check_table(instance, table_oids)
        self.report_table_metrics(instance, results)

    def report_table_metrics(self, instance, results):
        tags = instance.get("tags", [])
        tags = tags + ["snmp_device:"+instance.get('ip_address')]

        for metric in instance.get('metrics', []):
            if 'table' in metric:
                index_tags = []
                column_tags = []
                for metric_tag in metric.get('metric_tags', []):
                    tag_key = metric_tag['tag']
                    if 'index' in metric_tag:
                        index_tags.append((tag_key, metric_tag.get('index')))
                    if 'column' in metric_tag:
                        column_tags.append((tag_key, metric_tag.get('column')))

                for value_to_collect in metric.get("symbols", []):
                    for index, val in results[value_to_collect].items():
                        tag_for_this_index = tags + ["{0}:{1}".format(idx_tag[0], index[idx_tag[1] - 1]) for idx_tag in index_tags]
                        tag_for_this_index.extend(["{0}:{1}".format(col_tag[0], results[col_tag[1]][index]) for col_tag in column_tags])

                        self.submit_metric(value_to_collect, val, tag_for_this_index)

            elif 'symbol' in metric:
                name = metric['symbol']
                for _, val in results[name].items():
                    self.submit_metric(name, val, tags)


    def submit_metric(self, name, snmp_value, tags=[]):
        '''
        Convert the values reported as pysnmp-Managed Objects to values and
        report them to the aggregator
        '''
        if reply_invalid(snmp_value):
            # Metrics not present in the queried object
            self.log.warning("No such Mib available: %s" % name)
            return

        metric_name = "snmp." + name

        self.log.warning("metric: %s\nvalue: %s\ntags: %s\n\n", metric_name, snmp_value, tags)
        # Ugly hack but couldn't find a cleaner way
        # Proper way would be to use the ASN1 method isSameTypeWith but this
        # returns True in the case of CounterBasedGauge64 and Counter64 for example
        snmp_class = snmp_value.__class__.__name__
        for counter_class in SNMP_COUNTERS:
            if snmp_class==counter_class:
                value = int(snmp_value)
                self.rate(name, value, tags)
                return
        for gauge_class in SNMP_GAUGES:
            if snmp_class==gauge_class:
                value = int(snmp_value)
                self.gauge(name, value, tags)
                return
        self.log.warning("Unsupported metric type %s", snmp_class)

