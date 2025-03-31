# Support for feeder cabinet via CAN bus
#
# Copyright (C) 2023  <Your Name>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

# 状态定义
STATE_IDLE = 0      # 空闲状态
STATE_RUNOUT = 1    # 检测到耗材用尽
STATE_REQUESTING = 2 # 发送补料请求
STATE_FEEDING = 3   # 送料中
STATE_LOADED = 4    # 检测到新耗材
STATE_COMPLETE = 5  # 完成
STATE_ERROR = 6     # 错误状态

# 命令类型
CMD_REQUEST_FEED = 0x01  # 请求补料
CMD_STOP_FEED = 0x02     # 停止补料
CMD_QUERY_STATUS = 0x03  # 状态查询
CMD_PRINTING = 0x04      # 打印中
CMD_PRINT_COMPLETE = 0x05 # 打印完成
CMD_PRINT_PAUSE = 0x06   # 打印暂停
CMD_PRINT_CANCEL = 0x07  # 打印取消
CMD_PRINTER_IDLE = 0x08  # 打印机空闲

# 状态码
STATUS_IDLE = 0x00       # 空闲
STATUS_PREPARING = 0x01   # 准备送料
STATUS_FEEDING = 0x02     # 送料中
STATUS_COMPLETE = 0x03    # 送料完成
STATUS_FAILED = 0x04      # 送料失败

# 错误码
ERROR_NONE = 0x00         # 无错误
ERROR_MECHANICAL = 0x01    # 机械故障
ERROR_NO_FILAMENT = 0x02   # 耗材缺失
ERROR_OTHER = 0x03         # 其他错误

class FeederCabinet:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        
        # 配置CAN通信
        self.canbus_uuid = config.get('canbus_uuid')
        self.can_interface = config.get('can_interface', 'can1')
        
        # 状态变量
        self.state = STATE_IDLE
        self.progress = 0
        self.error_code = ERROR_NONE
        self.extruder_num = 0  # 默认第一个挤出头
        
        # 注册事件处理器
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        
        # 注册G-code命令
        self.gcode.register_command(
            'START_FEEDER_CABINET', self.cmd_START_FEEDER_CABINET,
            desc="手动启动续料")
        self.gcode.register_command(
            'QUERY_FEEDER_CABINET', self.cmd_QUERY_FEEDER_CABINET,
            desc="查询当前状态")
        self.gcode.register_command(
            'CANCEL_FEEDER_CABINET', self.cmd_CANCEL_FEEDER_CABINET,
            desc="取消续料操作")
        
        # 日志记录器
        self.logger = logging.getLogger(self.name)
    
    def _handle_connect(self):
        # 连接时初始化CAN通信
        self.canbus = None  # 确保canbus属性始终存在
        try:
            # 验证canbus_uuid格式
            if not self.canbus_uuid:
                raise self.printer.config_error("Missing required canbus_uuid parameter")
            
            try:
                # 尝试将canbus_uuid转换为16进制数，验证格式
                uuid_int = int(self.canbus_uuid, 16)
                if uuid_int < 0 or uuid_int > 0xffffffffffff:
                    raise ValueError("Invalid UUID format")
            except ValueError:
                raise self.printer.config_error(
                    "Invalid canbus_uuid format '%s'. Must be a valid hexadecimal string (e.g. F01000000601)" 
                    % (self.canbus_uuid,))
            
            # 获取canbus_ids对象来管理CAN总线节点ID
            try:
                canbus_ids = self.printer.lookup_object('canbus_ids')
            except self.printer.config_error:
                raise self.printer.config_error(
                    "CAN bus support not enabled. Check if [canbus_ids] section is present in config")
            
            # 获取节点ID
            try:
                self.nodeid = canbus_ids.get_nodeid(self.canbus_uuid)
            except self.printer.config_error:
                raise self.printer.config_error(
                    "Unknown canbus_uuid '%s'. Make sure this device is properly registered in your config" 
                    % (self.canbus_uuid,))
            
            self.mcu = self.printer.lookup_object('mcu')
            self.send_id = self.nodeid * 2 + 256
            self.receive_id = self.nodeid * 2 + 256 + 1
            
            # 获取实际的CAN总线接口对象
            self.canbus = self.mcu  # 使用mcu对象进行CAN通信
            self.logger.info("FeederCabinet initialized with nodeid %d", self.nodeid)
        except self.printer.config_error as e:
            self.logger.error("Failed to initialize CAN communication: %s", str(e))
            self.gcode.respond_info("错误: 送料柜CAN通信初始化失败 - %s" % str(e))
            self.state = STATE_ERROR
            self.error_code = ERROR_OTHER
        except Exception as e:
            self.logger.error("Failed to initialize CAN communication: %s", str(e))
            self.gcode.respond_info("错误: 送料柜CAN通信初始化失败 - %s" % str(e))
            self.state = STATE_ERROR
            self.error_code = ERROR_OTHER
    
    def _handle_ready(self):
        # 打印机就绪时设置为空闲状态
        self.state = STATE_IDLE
        self.progress = 0
        self.error_code = ERROR_NONE
        self.logger.info("FeederCabinet ready")
    
    def send_message(self, cmd_type, extruder_num=0):
        # 发送CAN消息到送料柜
        try:
            # 检查mcu是否已初始化
            if self.mcu is None:
                self.logger.error("Cannot send message: MCU not initialized")
                return False
                
            msg = bytearray(8)  # CAN消息固定8字节
            msg[0] = cmd_type
            msg[1] = extruder_num
            # 其余字节保留为0
            
            # 通过MCU发送CAN消息
            # 注意：这里需要根据实际的Klipper CAN通信API进行调整
            # 这是一个示例实现，可能需要根据实际情况修改
            cmd_fmt = "send_can_message oid=%d can_id=%d data=%s"
            cmd_params = {
                'oid': 0,  # 这里需要一个有效的OID，可能需要在初始化时获取
                'can_id': self.send_id,
                'data': ' '.join(['%02x' % b for b in msg])
            }
            self.mcu.get_printer().lookup_object('gcode').run_script(cmd_fmt % cmd_params)
            self.logger.debug("Sent message: cmd=%d, extruder=%d", cmd_type, extruder_num)
            return True
        except Exception as e:
            self.logger.error("Failed to send message: %s", str(e))
            return False
    
    def process_received_message(self, msg):
        # 处理从送料柜接收到的消息
        if len(msg) < 3:
            self.logger.error("Received invalid message (too short)")
            return
        
        status = msg[0]
        progress = msg[1]
        error_code = msg[2]
        
        self.logger.debug("Received message: status=%d, progress=%d, error=%d", 
                         status, progress, error_code)
        
        # 更新状态
        if status == STATUS_IDLE:
            self.state = STATE_IDLE
        elif status == STATUS_PREPARING:
            self.state = STATE_REQUESTING
        elif status == STATUS_FEEDING:
            self.state = STATE_FEEDING
        elif status == STATUS_COMPLETE:
            self.state = STATE_LOADED
        elif status == STATUS_FAILED:
            self.state = STATE_ERROR
        
        self.progress = progress
        self.error_code = error_code
        
        # 处理状态变化
        if self.state == STATE_LOADED:
            # 检测到新耗材，发送完成信号
            self.state = STATE_COMPLETE
            self.gcode.respond_info("续料完成，新耗材已加载")
        elif self.state == STATE_ERROR:
            # 处理错误
            error_msg = "未知错误"
            if error_code == ERROR_MECHANICAL:
                error_msg = "机械故障"
            elif error_code == ERROR_NO_FILAMENT:
                error_msg = "耗材缺失"
            elif error_code == ERROR_OTHER:
                error_msg = "其他错误"
            
            self.gcode.respond_info("续料失败: %s" % error_msg)
    
    def start_feeding(self, extruder_num=0):
        # 开始送料流程
        if self.state != STATE_IDLE and self.state != STATE_RUNOUT:
            self.gcode.respond_info("无法启动续料：当前状态不允许此操作")
            return False
        
        self.extruder_num = extruder_num
        self.state = STATE_REQUESTING
        self.progress = 0
        
        # 发送补料请求
        if not self.send_message(CMD_REQUEST_FEED, extruder_num):
            self.state = STATE_ERROR
            self.error_code = ERROR_OTHER
            self.gcode.respond_info("发送补料请求失败")
            return False
        
        self.gcode.respond_info("已发送补料请求，等待送料柜响应")
        return True
    
    def cancel_feeding(self):
        # 取消送料流程
        if self.state == STATE_IDLE or self.state == STATE_COMPLETE:
            self.gcode.respond_info("当前没有活动的续料操作")
            return False
        
        # 发送停止补料命令
        if not self.send_message(CMD_STOP_FEED, self.extruder_num):
            self.gcode.respond_info("发送取消命令失败")
            return False
        
        self.state = STATE_IDLE
        self.progress = 0
        self.gcode.respond_info("已取消续料操作")
        return True
    
    def query_status(self):
        # 查询当前状态
        state_desc = "未知"
        if self.state == STATE_IDLE:
            state_desc = "空闲"
        elif self.state == STATE_RUNOUT:
            state_desc = "检测到耗材用尽"
        elif self.state == STATE_REQUESTING:
            state_desc = "发送补料请求"
        elif self.state == STATE_FEEDING:
            state_desc = "送料中"
        elif self.state == STATE_LOADED:
            state_desc = "检测到新耗材"
        elif self.state == STATE_COMPLETE:
            state_desc = "完成"
        elif self.state == STATE_ERROR:
            state_desc = "错误"
        
        error_msg = ""
        if self.error_code != ERROR_NONE:
            if self.error_code == ERROR_MECHANICAL:
                error_msg = "机械故障"
            elif self.error_code == ERROR_NO_FILAMENT:
                error_msg = "耗材缺失"
            elif self.error_code == ERROR_OTHER:
                error_msg = "其他错误"
            error_msg = "错误信息：" + error_msg
        
        self.gcode.respond_info(
            "当前状态：%s\n进度：%d%%\n%s" % 
            (state_desc, self.progress, error_msg))
        
        # 同时发送状态查询命令到送料柜以更新状态
        self.send_message(CMD_QUERY_STATUS, self.extruder_num)
        
        return True
    
    # G-code命令处理函数
    def cmd_START_FEEDER_CABINET(self, gcmd):
        extruder_num = gcmd.get_int('EXTRUDER', 0, minval=0, maxval=1)
        self.start_feeding(extruder_num)
    
    def cmd_QUERY_FEEDER_CABINET(self, gcmd):
        self.query_status()
    
    def cmd_CANCEL_FEEDER_CABINET(self, gcmd):
        self.cancel_feeding()

def load_config(config):
    return FeederCabinet(config)