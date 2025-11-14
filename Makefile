TOP := $(CURDIR)
PYTHON = python
SIMULATOR = ghdl
BUILD_DIR = build
include CONFIG

test:
	panda_src_dir=$(FPGA) \
	    $(PYTHON) dev-tests/test_pcap_dma.py
	#panda_src_dir=$(FPGA) $(PYTHON) -m pytest dev-tests
