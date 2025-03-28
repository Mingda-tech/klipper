# 导入必要的库
import logging
import struct
import queue
import threading
from . import bus
from . import canbus

class CanFeederInterface:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.can_interface = config.get('can_interface', 'can0')
        self.uuid = int(config.get('canbus_uuid'), 16)
        
        # CAN通信参数
        self.mcu = canbus.MCU_canbus_query(config, self.can_interface)
        self.cmd_queue = queue.Queue()
        self.rx_queue = queue.Queue()
        self.mutex = threading.Lock()
        
        # 注册CAN接收回调
        self.mcu.register_callback(self.handle_can_message)
        
        # CAN消息ID定义
        self.CMD_ID_BASE = 0x100  # 命令帧基础ID
        self.STATUS_ID_BASE = 0x200  # 状态帧基础ID
        
        # 命令类型定义
        self.CMD_TYPES = {
            'LOAD': 0x01,
            'UNLOAD': 0x02,
            'STATUS': 0x03,
            'CONTINUOUS_LOAD': 0x04,
            'STOP': 0x05,
            'SWITCH_TRAY': 0x06,
            'PRINTING': 0x07,
            'IDLE': 0x08,
            'PAUSE': 0x09,
            'CANCEL': 0x0A,
            'COMPLETE': 0x0B
        }
        
        # 状态处理回调字典
        self.status_handlers = {}
        
    def send_command(self, cmd_type, tray_id, params=None):
        """发送CAN命令
        Args:
            cmd_type: 命令类型（参考CMD_TYPES）
            tray_id: 料仓号（1-256）
            params: 附加参数（最多4字节）
        """
        if params is None:
            params = 0
            
        # 构建CAN消息
        msg_id = self.CMD_ID_BASE | (cmd_type & 0xFF)
        data = bytearray(8)  # 8字节数据
        
        # 填充数据
        data[0] = cmd_type & 0xFF
        data[1] = (tray_id >> 8) & 0xFF
        data[2] = tray_id & 0xFF
        
        # 填充参数（4字节）
        param_bytes = struct.pack('>I', params)
        data[3:7] = param_bytes
        
        # 计算校验和
        checksum = sum(data[0:7]) & 0xFF
        data[7] = checksum
        
        try:
            with self.mutex:
                # 发送CAN消息
                self.mcu.send_raw(msg_id, data)
                logging.info(f"CAN command sent: ID={hex(msg_id)}, Data={[hex(x) for x in data]}")
                return True
        except Exception as e:
            logging.error(f"Failed to send CAN command: {e}")
            return False
            
    def handle_can_message(self, msg_id, data):
        """处理接收到的CAN消息
        Args:
            msg_id: CAN消息ID
            data: 消息数据（8字节）
        """
        if (msg_id & 0xF00) != self.STATUS_ID_BASE:
            return
            
        try:
            # 验证校验和
            calc_checksum = sum(data[0:7]) & 0xFF
            if calc_checksum != data[7]:
                logging.error("CAN message checksum error")
                return
                
            # 解析状态信息
            status_code = data[0]
            current_action = data[1]
            tray_id = (data[2] << 8) | data[3]
            extra_info = struct.unpack('>I', data[4:7])[0]
            
            # 创建状态数据包
            status_data = {
                'status_code': status_code,
                'action': current_action,
                'tray_id': tray_id,
                'extra_info': extra_info
            }
            
            # 将状态放入接收队列
            self.rx_queue.put(status_data)
            
            # 调用对应的状态处理器
            if status_code in self.status_handlers:
                self.status_handlers[status_code](status_data)
                
        except Exception as e:
            logging.error(f"Error handling CAN message: {e}")
            
    def register_status_handler(self, status_code, handler):
        """注册状态处理回调函数"""
        self.status_handlers[status_code] = handler
        
    def get_last_status(self):
        """获取最新的状态信息"""
        try:
            return self.rx_queue.get_nowait()
        except queue.Empty:
            return None
            
    def handle_error(self, error_code):
        """处理错误"""
        logging.error(f"CAN communication error: {hex(error_code)}")
        # 实现错误恢复逻辑