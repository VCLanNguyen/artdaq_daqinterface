
# JCF, Jun-19-2017

# This file contains the names of stubbed-out (empty) functions for
# which experiments may want to create their own versions in separate
# *.py files. Do not edit this file. 

# JCF, Feb-7-2019

# As evidenced by the bookkeeping function I've added, another use
# case for this module is if you want to disable a function in
# DAQInterface; in that case, swap out the function's original module
# for this module when doing imports at the top of daqinterface.py

def start_datataking_base(self):
    pass

def stop_datataking_base(self):
    pass

def do_enable_base(self):
    pass

def do_disable_base(self):
    pass

def bookkeeping_for_fhicl_documents_artdaq_v3_base(self):
    pass
