# 导入必要的库
import logging
from . import can_feeder

class FeederCabinet:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        
        # 配置参数
        self.tray_count = config.getint('tray_count', 4)
        self.default_speed = config.getint('default_speed', 100)
        self.timeout = config.getint('timeout', 1000)
        
        # 初始化CAN接口
        self.can_interface = can_feeder.CanFeederInterface(config)
        
        # 状态变量
        self.current_tray = 0
        self.is_busy = False
        self.error_state = None
        
        # 注册G-code命令
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('FEEDER_LOAD', self.cmd_FEEDER_LOAD,
                                  desc="Load filament from specified tray")
        self.gcode.register_command('FEEDER_UNLOAD', self.cmd_FEEDER_UNLOAD,
                                  desc="Unload filament from specified tray")
        self.gcode.register_command('FEEDER_STATUS', self.cmd_FEEDER_STATUS,
                                  desc="Query feeder cabinet status")
                                  
        # 注册状态处理器
        self.can_interface.register_status_handler(0x00, self._handle_normal_status)
        self.can_interface.register_status_handler(0x01, self._handle_busy_status)
        self.can_interface.register_status_handler(0x02, self._handle_error_status)
        
    def load_filament(self, tray_id, length=None):
        """从指定料仓加载耗材
        Args:
            tray_id: 料仓号
            length: 进料长度（可选）
        """
        if not 1 <= tray_id <= self.tray_count:
            raise self.printer.command_error(f"Invalid tray ID: {tray_id}")
            
        if self.is_busy:
            raise self.printer.command_error("Feeder is busy")
            
        params = length if length is not None else self.default_speed
        
        # 发送进料命令
        success = self.can_interface.send_command(
            self.can_interface.CMD_TYPES['LOAD'],
            tray_id,
            params
        )
        
        if not success:
            raise self.printer.command_error("Failed to send load command")
            
        self.is_busy = True
        self.current_tray = tray_id
        
    def unload_filament(self, tray_id):
        """从指定料仓退料
        Args:
            tray_id: 料仓号
        """
        if not 1 <= tray_id <= self.tray_count:
            raise self.printer.command_error(f"Invalid tray ID: {tray_id}")
            
        if self.is_busy:
            raise self.printer.command_error("Feeder is busy")
            
        # 发送退料命令
        success = self.can_interface.send_command(
            self.can_interface.CMD_TYPES['UNLOAD'],
            tray_id,
            0
        )
        
        if not success:
            raise self.printer.command_error("Failed to send unload command")
            
        self.is_busy = True
        self.current_tray = tray_id
        
    def _handle_normal_status(self, status_data):
        """处理正常状态"""
        self.is_busy = False
        self.error_state = None
        
    def _handle_busy_status(self, status_data):
        """处理忙碌状态"""
        self.is_busy = True
        
    def _handle_error_status(self, status_data):
        """处理错误状态"""
        self.is_busy = False
        self.error_state = status_data['extra_info']
        logging.error(f"Feeder error: {hex(self.error_state)}")
        
    def cmd_FEEDER_LOAD(self, gcmd):
        """处理FEEDER_LOAD G-code命令"""
        tray_id = gcmd.get_int('TRAY', None)
        if tray_id is None:
            raise gcmd.error("TRAY parameter is required")
            
        length = gcmd.get_float('LENGTH', None)
        try:
            self.load_filament(tray_id, length)
            gcmd.respond_info(f"Loading filament from tray {tray_id}")
        except Exception as e:
            raise gcmd.error(str(e))
            
    def cmd_FEEDER_UNLOAD(self, gcmd):
        """处理FEEDER_UNLOAD G-code命令"""
        tray_id = gcmd.get_int('TRAY', None)
        if tray_id is None:
            raise gcmd.error("TRAY parameter is required")
            
        try:
            self.unload_filament(tray_id)
            gcmd.respond_info(f"Unloading filament from tray {tray_id}")
        except Exception as e:
            raise gcmd.error(str(e))
            
    def cmd_FEEDER_STATUS(self, gcmd):
        """处理FEEDER_STATUS G-code命令"""
        status = self.can_interface.get_last_status()
        if status is None:
            gcmd.respond_info("No status available")
            return
            
        gcmd.respond_info(
            f"Feeder status:\n"
            f"Current tray: {self.current_tray}\n"
            f"Busy: {self.is_busy}\n"
            f"Error state: {hex(self.error_state) if self.error_state else 'None'}\n"
            f"Last status: {status}"
        )

def load_config(config):
    return FeederCabinet(config)