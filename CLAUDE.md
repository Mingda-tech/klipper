# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Klipper is a 3D printer firmware that combines the power of a general-purpose computer (like a Raspberry Pi) with one or more microcontrollers. The host software (Klippy) runs on the computer and handles complex calculations, while lightweight real-time code runs on the microcontroller(s).

## Development Commands

### Building MCU Code
```bash
# Configure for a specific board (interactive menu)
make menuconfig

# Build MCU firmware
make

# Clean build artifacts
make clean
make distclean  # Also removes config
```

### Host Software
```bash
# Install Klippy dependencies
sudo scripts/install-debian.sh  # or appropriate distro script

# Run regression tests
python2 test/klippy/*.py
scripts/test_klippy.py test/klippy/

# Run Klippy directly for development
~/klippy-env/bin/python klippy/klippy.py printer.cfg -l /tmp/klippy.log
```

### Testing
- MCU configs for testing: `test/configs/`
- Klippy regression tests: `test/klippy/*.test` files with matching `.cfg` configs
- Test runner: `scripts/test_klippy.py`
- CI build script: `scripts/ci-build.sh`

## Architecture Overview

### Two-Part Architecture
1. **Host Software (Klippy)** - Python code in `klippy/`
   - Main entry: `klippy/klippy.py`
   - G-code parsing: `klippy/gcode.py`
   - Motion planning: `klippy/toolhead.py`
   - MCU communication: `klippy/mcu.py`
   - Kinematics: `klippy/kinematics/`
   - Extensions: `klippy/extras/`

2. **MCU Firmware** - C code in `src/`
   - Architecture-specific: `src/avr/`, `src/stm32/`, `src/rp2040/`, etc.
   - Generic helpers: `src/generic/`
   - Main scheduler: `src/sched.c`
   - Command handling: `src/command.c`

### Key Components
- **Scheduler**: Real-time task scheduling with `DECL_INIT()`, `DECL_TASK()`, `DECL_COMMAND()` macros
- **Communication**: Binary protocol between host and MCU via `klippy/msgproto.py`
- **Motion**: Trapezoid motion planning with lookahead in `klippy/toolhead.py`
- **Kinematics**: Pluggable kinematics systems (cartesian, delta, corexy, etc.)
- **Extensions**: Modular extras system for features like bed leveling, input shaping, etc.

### Configuration System
- Uses Kconfig for MCU build configuration (`src/Kconfig`)
- Runtime configuration via `.cfg` files (examples in `config/`)
- Configuration parsing in `klippy/configfile.py`

## Directory Structure

- `src/` - MCU firmware (C code)
- `klippy/` - Host software (Python)
- `klippy/chelper/` - C extensions for host
- `lib/` - External libraries and MCU-specific headers  
- `config/` - Example printer configurations
- `scripts/` - Build and utility scripts
- `docs/` - Documentation (also see docs/Code_Overview.md)
- `test/` - Test configurations and regression tests

## Development Guidelines

### Code Style
- MCU code: Follow existing C style, use macros for declarations
- Host code: Python style, modules in `klippy/extras/` for extensions
- Timer functions must complete in microseconds
- Task functions should avoid delays >100μs
- Use `shutdown()` for error conditions

### Adding Features
1. MCU commands: Add to appropriate `src/` files with `DECL_COMMAND()`
2. Host features: Create modules in `klippy/extras/`
3. New kinematics: Add to `klippy/kinematics/`
4. Configuration: Add to relevant sections, document in Config_Reference.md

### Testing
- Test MCU builds with `make` using configs from `test/configs/`
- Add regression tests to `test/klippy/` for new features
- Use `scripts/test_klippy.py` to run test suites
- All code paths should be testable without hardware

## Protocol Communication
- Binary protocol between host and MCU defined in `docs/Protocol.md`
- Commands are sent from host to MCU
- MCU responses are handled in `klippy/mcu.py`
- Real-time constraints on MCU side, complex logic on host side