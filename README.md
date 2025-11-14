# PandABlocks simulator based on cocotb

## Requirements
- [ ] Req 1: The simulator result must be based on a block VHDL code simulation.
- [ ] Req 2: It should be able to simulate changes in inputs at 10hz.

## Analysis
- How to isolate the MVP as much as possible? Interface should only concern
  about the fields (no need to complicate parsing registers/config).
  Additionally, the sim engine could offer a socket interface with very simple
  commands, e.g. `ENABLE=1` or `ENABLE?`
- A possible simplification is creating a sim target to take advantage of
  autogeneration framework.
- We probably can simulate just a few ticks, advance timers and wait until
  next change in input.

## Design Notes

### High Level
![](./panda-cocotb-sim-design.drawio.png)

### Low Level

## Implementation Notes
- 9999 protocol summary
  - Read one register: `'R' + block(1B) + instance(1B) + reg(1B)`
    - response with 4-byte value
  - Write one register: `'W' + block(1B) + instance(1B) + reg(1B) +
    value(4B)`
    - no response
  - Table write:
    `'T' + block(1B) + instance(1B) + reg(1B) + length(4B) + data(length*4 B)`
    - no response
  - Data fetching: `'D' + dummy(1B) + dummy(1B) + dummy(1B) + length(4B)`
    - response: `length(4B) + data(length B)`, to signal end, just send -1 as
      length and no data.
- It's cleaner creating a test target and not dealing with register decoding,
  I'm working on that, but in the process I need to adapt some of the entities,
  for example, replacing IP-based fifo with vhdl fifo.

## Test Notes
- A very basic test for pcap\_dma was added, this is also a good base to add
  more test.
