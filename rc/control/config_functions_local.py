
import os
import sys
sys.path.append( os.environ["ARTDAQ_DAQINTERFACE_DIR"] )

import re
import traceback
import shutil
import subprocess
from subprocess import Popen

from rc.control.utilities import expand_environment_variable_in_string
from rc.control.utilities import make_paragraph

def get_config_parentdir():
    parentdir = os.environ["DAQINTERFACE_FHICL_DIRECTORY"]
    assert os.path.exists(parentdir), "Expected configuration directory %s doesn't appear to exist" % (parentdir)
    return parentdir


def get_config_info_base(self):

    uuidgen=Popen("uuidgen", shell=True, stdout=subprocess.PIPE).stdout.readlines()[0].strip()
    tmpdir = "/tmp/%s" % (uuidgen)
    os.mkdir(tmpdir)

    ffp = []

    if os.path.exists( "%s/common_code" % get_config_parentdir() ):
        self.subconfigs_for_run.append( "common_code" ) # For backwards-compatibility with earlier versions of this function

    for subconfig in self.subconfigs_for_run:
        subconfig_dir = "%s/%s" % (get_config_parentdir(), subconfig)
        
        if os.path.exists( subconfig_dir ):
            tmp_subconfig_dir = "%s/%s" % (tmpdir, subconfig)
            shutil.copytree( subconfig_dir, tmp_subconfig_dir )
            assert os.path.exists( tmp_subconfig_dir )
            ffp.append( tmp_subconfig_dir )
        else:
            raise Exception(make_paragraph("Error: unable to find expected directory of FHiCL configuration files \"%s\"" % (subconfig_dir) ))

    return tmpdir, ffp

# put_config_info_base should be a no-op 

def put_config_info_base(self):
    pass

def get_daqinterface_config_info_base(self, daqinterface_config_filename):

    inf = open(daqinterface_config_filename)

    if not inf:
        raise Exception(self.make_paragraph(
                            "Exception in DAQInterface: " +
                            "unable to locate configuration file \"" +
                            daqinterface_config_filename + "\""))

    memberDict = {"name": None, "label": None, "host": None, "port": "not set", "fhicl": None}

    num_expected_processes = 0
    num_actual_processes = 0

    lines = inf.readlines()
    for i_line, line in enumerate(lines):

        line = expand_environment_variable_in_string( line )

        res = re.search(r"^\s*PMT host\s*:\s*(\S+)", line)
        if res:
            self.pmt_host = res.group(1)
            continue

        res = re.search(r"^\s*PMT port\s*:\s*(\S+)", line)
        if res:
            self.pmt_port = res.group(1)
            continue

        res = re.search(r"^\s*DAQ setup script\s*:\s*(\S+)",
                        line)
        if res:
            self.daq_setup_script = res.group(1)
            self.daq_dir = os.path.dirname( self.daq_setup_script ) + "/"
            continue

        res = re.search(r"^\s*tcp_base_port\s*:\s*(\S+)",
                        line)
        if res:
            raise Exception(make_paragraph("Jun-29-2018: the variable \"tcp_base_port\" was found in the boot file %s; this use is deprecated as tcp port values are now set internally in artdaq since artdaq commit d338b810c589a177ff1a34d82fa82a459cc1704b" % (daqinterface_config_filename)))

        res = re.search(r"^\s*request_port\s*:\s*(\S+)",
                        line)
        if res:
            self.request_port = int( res.group(1) )
            continue

        res = re.search(r"^\s*request_address\s*:\s*(\S+)",
                        line)
        if res:
            self.request_address = res.group(1)
            continue

        res = re.search(r"^\s*table_update_address\s*:\s*(\S+)",
                        line)
        if res:
            self.table_update_address = res.group(1)
            continue

        res = re.search(r"^\s*routing_base_port\s*:\s*(\S+)",
                        line)
        if res:
            self.routing_base_port = res.group(1)
            continue

        res = re.search(r"^\s*partition_number\s*:\s*(\S+)",
                        line)
        if res:
            raise Exception(make_paragraph("Jun-24-2018: the variable \"partition_number\" was found in the boot file %s; this use is deprecated as \"partition_number\" is now set by the DAQINTERFACE_PARTITION_NUMBER environment variable" % (daqinterface_config_filename)))

        res = re.search(r"^\s*debug level\s*:\s*(\S+)",
                        line)
        if res:
            self.debug_level = int(res.group(1))
            continue

        res = re.search(r"^\s*manage processes\s*:\s*[tT]rue",
                        line)
        if res:
            self.manage_processes = True
            continue

        res = re.search(r"^\s*manage processes\s*:\s*[fF]alse",
                        line)
        if res:
            self.manage_processes = False
            continue

        res = re.search(r"^\s*disable recovery\s*:\s*[tT]rue",
                        line)
        if res:
            self.disable_recovery = True
            continue

        res = re.search(r"^\s*disable recovery\s*:\s*[fF]alse",
                        line)
        if res:
            self.disable_recovery = False
            continue

        if "EventBuilder" in line or \
                "DataLogger" in line or "Dispatcher" in line or \
                "RoutingMaster" in line:

            res = re.search(r"^\s*(\w+)\s+(\S+)\s*:\s*(\S+)", line)

            if res:
                memberDict["name"] = res.group(1)
                memberDict[res.group(2)] = res.group(3)

                if res.group(2) == "host":
                    num_expected_processes += 1

        # Taken from Eric: if a line is blank or a comment or we've
        # reached the last line in the boot file, check to see if
        # we've got a complete set of info for an artdaq process

        if re.search(r"^\s*#", line) or re.search(r"^\s*$", line) or \
           i_line == len(lines) - 1:

            filled = True

            for key, value in memberDict.items():
                if value is None and not key == "fhicl":
                    filled = False

            # If it has been filled, then initialize a Procinfo
            # object, append it to procinfos, and reset the
            # dictionary values to null strings

            if filled:

                num_actual_processes += 1
                
                if memberDict["port"] == "not set":
                    
                    # Not necessarily the same as artdaq's idea of rank...
                    rank = len(self.daq_comp_list) + num_actual_processes - 1
                           
                    memberDict["port"] = str( int(os.environ["ARTDAQ_BASE_PORT"]) + \
                                              100 + \
                                              self.partition_number*int(os.environ["ARTDAQ_PORTS_PER_PARTITION"]) + \
                                              rank )

                self.procinfos.append(self.Procinfo(memberDict["name"],
                                                    memberDict["host"],
                                                    memberDict["port"],
                                                    memberDict["label"]))

                for varname in memberDict.keys():
                    if varname != "port":
                        memberDict[varname] = None
                    else:
                        memberDict[varname] = "not set"

    # Unless I'm mistaken, we don't yet have an official default for
    # the pmt port given a partition #

    if not hasattr(self, "pmt_port") or self.pmt_port is None:
        self.pmt_port = str( int(self.rpc_port) + 1 )

    if num_expected_processes != num_actual_processes:
        raise Exception(make_paragraph("An inconsistency exists in the boot file; a host was defined in the file for %d artdaq processes, but there's only a complete set of info in the file for %d processes. This may be the result of using a boot file designed for an artdaq version prior to the addition of a label requirement (see https://cdcvs.fnal.gov/redmine/projects/artdaq-utilities/wiki/The_boot_file_reference for more)" % (num_expected_processes, num_actual_processes)))


    return daqinterface_config_filename

def listdaqcomps_base(self):

    components_file = os.environ["DAQINTERFACE_KNOWN_BOARDREADERS_LIST"]

    try:
        inf = open( components_file )
    except:
        print traceback.format_exc()
        return 

    lines = inf.readlines()

    print
    print "# of components found in listdaqcomps call: %d" % (len(lines))

    lines.sort()
    for line in lines:
        component = line.split()[0].strip()
        host = line.split()[1].strip()
        
        print "%s (runs on %s)" % (component, host)

def listconfigs_base(self):
    subdirs = next(os.walk(get_config_parentdir()))[1]
    configs = [subdir for subdir in subdirs if subdir != "common_code" ]

    listconfigs_file="/tmp/listconfigs_" + os.environ["USER"] + ".txt"

    outf = open(listconfigs_file, "w")

    print
    print "Available configurations: "
    for config in sorted(configs):
        print config
        outf.write("%s\n" % config)

    print
    print "See file \"%s\" for saved record of the above configurations" % (listconfigs_file)
    print make_paragraph("Please note that for the time being, the optional max_configurations_to_list variable which may be set in %s is only applicable when working with the database" % os.environ["DAQINTERFACE_SETTINGS"])
    print

def main():
    print "Calling listdaqcomps_base: "
    listdaqcomps_base("ignored_argument")

if __name__ == "__main__":
    main()
