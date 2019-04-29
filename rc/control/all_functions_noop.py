
# JCF, Jun-19-2017

# This file contains the names of stubbed-out (empty) functions for
# which experiments may want to create their own versions in separate
# *.py files. Do not edit this file. 

# JCF, Feb-7-2019

# As evidenced by the bookkeeping function I've added, another use
# case for this module is if you want to disable a function in
# DAQInterface; in that case, swap out the function's original module
# for this module when doing imports at the top of daqinterface.py

# JCF, Apr-2-2019

# Alphabetized the functions so it's easy for developers to look them
# up; this is possible since I don't need to worry about function
# definition order like I might if these weren't no-op

def bookkeeping_for_fhicl_documents_artdaq_v3_base(self):
    pass
def check_proc_heartbeats_base(self):
    pass
def do_disable_base(self):
    pass
def do_enable_base(self):
    pass
def find_process_manager_variable_base():
    pass
def get_pid_for_process_base(self):
    pass
def get_process_manager_log_filenames_base(self):
    pass
def kill_procs_base(self):
    pass
def launch_procs_base(self):
    pass
def mopup_process_base(self):
    pass
def process_launch_diagnostics_base(self):
    pass
def process_manager_cleanup_base(self):
    pass
def reset_process_manager_variables_base(self):
    pass
def set_process_manager_default_variables_base(self):
    pass
def softlink_process_manager_logfiles_base(self):
    pass
def start_datataking_base(self):
    pass
def stop_datataking_base(self):
    pass



