#!/usr/bin/env python
import cocotb
import logging
import numpy as np

from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, ClockCycles, Event
from cocotb_bus.monitors import BusMonitor
from cocotb_bus.drivers import BusDriver
from cocotb_tools.runner import get_runner
# can't use this yet until we move to AXI4
#from cocotbext.axi import AxiWriteBus, AxiSlaveWrite

from collections import deque
from common import get_panda_path, get_extra_path
from typing import Any, Sequence

TOP_PATH = get_panda_path()
EXTRA_PATH = get_extra_path()


@cocotb.test()
async def can_run(dut):
    cocotb.start_soon(Clock(dut.clk_i, 1, 'ns').start(start_high=False))
    await RisingEdge(dut.clk_i)


class PcapDriver(BusDriver):
    _signals = [
        'pcap_dat_i',
        'pcap_wstb_i',
        'pcap_done_i'
    ]

    def __init__(self, dut, name, clock, **kwargs):
        super().__init__(dut, name, clock, **kwargs)
        self.bus.pcap_wstb_i.value = 0

    async def finish(self):
        self.bus.pcap_done_i.value = 1
        await RisingEdge(self.clock)
        self.bus.pcap_done_i.value = 0
        await RisingEdge(self.clock)

    async def _driver_send(self, transaction: Sequence[int], sync: bool = True,
                           **kwargs: Any) -> None:
        if sync:
            await RisingEdge(self.clock)

        self.bus.pcap_wstb_i.value = 1
        for data in transaction:
            self.bus.pcap_dat_i.value = data
            await RisingEdge(self.clock)

        self.bus.pcap_wstb_i.value = 0


class PcapAddressDriver(object):
    _signals = [
        'dma_addr',
        'dma_addr_wstb',
        'dma_init',
        'dma_reset',
    ]
    def __init__(self, dut, addresses, block_size, delay_in_irq, **kwargs):
        self.log = logging.getLogger(__class__.__name__)
        self.dut = dut
        self.clock = dut.clk_i
        self.block_size = block_size
        self.delay_in_irq = delay_in_irq
        assert len(addresses) > 1, "At least 2 addresses must be provided"
        self.addr_queue = deque(addresses)
        self.dut.dma_addr.value = 0
        self.dut.dma_reset.value = 0
        self.dut.dma_init.value = 0
        self.init_event = Event()
        self.task = cocotb.start_soon(self.run())

    async def wait_for_initialization(self):
        await self.init_event.wait()

    async def reset(self):
        await RisingEdge(self.clock)
        self.dut.dma_reset.value = 1
        await RisingEdge(self.clock)
        self.dut.dma_reset.value = 0
        await RisingEdge(self.clock)
        await RisingEdge(self.clock)

    async def push_address(self, addr):
        await RisingEdge(self.clock)
        self.log.debug('Pushing DMA address 0x%x', addr)
        self.dut.dma_addr.value = addr
        self.dut.dma_addr_wstb.value = 1
        await RisingEdge(self.clock)
        self.dut.dma_addr_wstb.value = 0
        await RisingEdge(self.clock)

    async def run(self):
        self.dut.block_size.value = self.block_size
        await self.reset()
        await self.push_address(self.addr_queue.popleft())
        assert self.dut.pcap_fsm.value == 0, "DMA FSM should be init"
        self.dut.dma_init.value = 1
        await RisingEdge(self.clock)
        self.dut.dma_init.value = 0
        await RisingEdge(self.clock)
        assert self.dut.pcap_fsm.value == 1, "DMA FSM should be active"
        await self.push_address(self.addr_queue.popleft())
        self.init_event.set()
        while self.addr_queue:
            await RisingEdge(self.dut.irq_o)
            await ClockCycles(self.clock, self.delay_in_irq)
            await self.push_address(self.addr_queue.popleft())


class Memory(object):
    def __init__(self, size):
        self.log = logging.getLogger(__class__.__name__)
        self.size = size
        self.mem = bytearray(size)
        self.word_view = np.frombuffer(self.mem, dtype=np.uint32)

    def clear(self):
        self.word_view.fill(0)

    def add_burst(self, addr, data_list):
        assert addr % 4 == 0, "Address must be word-aligned"
        index = addr // 4
        for data in data_list:
            self.word_view[index] = data
            index += 1

    def assert_content(self, addr, data_list):
        assert addr % 4 == 0, "Address must be word-aligned"
        index = addr // 4
        self.log.debug('Checking memory region - addr: 0x%08X, size: %d',
                       addr, len(data_list)*4)
        for expected in data_list:
            data = self.word_view[index]
            assert data == expected, \
                f'Memory mismatch at address 0x{index*4:08X}: ' + \
                f'expected 0x{expected:08X}, got 0x{data:08X}'
            index += 1


class AxiWriteSlave(BusMonitor):
    # Simplifications:
    # - address should arrive before the last data beat
    # - strobe is always all-ones
    _signals = [
        'awaddr',
        'awvalid',
        'awready',
        'wdata',
        'wstrb',
        'wvalid',
        'wready',
        'wlast',
        'bvalid',
        'bready',
        'bresp',
    ]

    def __init__(self, dut, name, clock, **kwargs):
        self.log = logging.getLogger(__class__.__name__)
        self.want_quit = False
        self.n_bursts = 0
        self.n_resp = 0
        super().__init__(dut, name, clock, **kwargs)

    async def _monitor_recv(self):
        self.bus.awready.value = 1
        self.bus.wready.value = 1
        self.bus.bvalid.value = 0
        self.bus.bresp.value = 0
        data_list = []
        addr = None
        need_resp = 0
        while not self.want_quit:
            await RisingEdge(self.clock)
            if self.bus.awvalid.value == 1 and self.bus.awready.value == 1:
                addr = self.bus.awaddr.value.to_unsigned()
                self.bus.awready.value = 0
                self.log.debug('AXI Write Slave received address 0x%08X', addr)

            if self.bus.wvalid.value:
                data_list.append(self.bus.wdata.value.to_unsigned())
                if self.bus.wlast.value:
                    self.log.debug(
                        'AXI Write Slave received burst of %d', len(data_list))
                    assert addr is not None, \
                        'Address should be set before last data beat'
                    self._recv((addr, data_list))
                    data_list = []
                    addr = None
                    need_resp += 1
                    self.n_bursts += 1
                    self.bus.awready.value = 1

            self.bus.bvalid.value = 1 if need_resp else 0
            if self.bus.bvalid.value and self.bus.bready.value:
                need_resp -= 1
                self.n_resp += 1
                if not need_resp:
                    self.bus.bvalid.value = 0


class TB(object):
    def __init__(self, dut, addresses, block_size, delay_in_irq=1):
        self.dut = dut
        self.clock = dut.clk_i
        self.slave = AxiWriteSlave(dut, 'm_axi', dut.clk_i)
        self.driver = PcapDriver(dut, '', dut.clk_i)
        self.addr_driver = PcapAddressDriver(dut, addresses, block_size,
                                             delay_in_irq)
        self.memory = Memory(size=0x00004000)
        def handle_write(transaction):
            addr, data_list = transaction
            self.memory.add_burst(addr, data_list)

        self.slave.add_callback(handle_write)

    def init_signals(self):
        for attr_name in [
                'pcap_start_event_i', 'pcap_dat_i', 'pcap_wstb_i', 'pcap_done_i',
                'pcap_status_i', 'dma_reset', 'dma_init', 'dma_addr',
                'dma_addr_wstb', 'timeout', 'timeout_wstb']:
            self.dut[attr_name].value = 0


@cocotb.test()
async def test_2_buffers_and_finish(dut):
    logging.basicConfig(level=logging.DEBUG, force=True)
    cocotb.start_soon(Clock(dut.clk_i, 1, 'ns').start(start_high=False))
    tb = TB(dut, addresses=[0x00001000, 0x00002000, 0x00003000],
            block_size=128, delay_in_irq=1)
    tb.init_signals()
    await tb.addr_driver.wait_for_initialization()
    await tb.driver.send(list(range(48)))
    await tb.driver.finish()
    await ClockCycles(tb.clock, 128)
    assert tb.slave.n_bursts == 3
    tb.memory.assert_content(0x00001000, list(range(0, 32)))
    tb.memory.assert_content(0x00002000, list(range(32, 48)))


def test_pcap_dma():
    runner = get_runner('ghdl')
    runner.build(sources=[
                     TOP_PATH / 'common' / 'hdl' / 'defines' / 'support.vhd',
                     EXTRA_PATH / 'top_defines_gen.vhd',
                     TOP_PATH / 'common' / 'hdl' / 'defines' /
                         'top_defines.vhd',
                     TOP_PATH / 'common' / 'hdl' / 'fifo.vhd',
                     TOP_PATH / 'modules' / 'pcap' / 'hdl' / 'axi_write_master.vhd',
                     TOP_PATH / 'modules' / 'pcap' / 'hdl' / 'pcap_dma.vhd',
                 ],
                 build_args=['--std=08'],
                 build_dir='sim_pcap_dma',
                 hdl_toplevel='pcap_dma',
                 always=True)
    runner.test(hdl_toplevel='pcap_dma',
                test_args=['--std=08'],
                plusargs = [
                    '--fst=pcap_dma.fst',
                    #'--vpi-trace=/proc/self/fd/0',
                ],
                verbose=True,
                test_module='test_pcap_dma')


def main():
    test_pcap_dma()


if __name__ == '__main__':
    main()
