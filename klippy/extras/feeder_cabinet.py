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
        self.can_interface = config.get('can_interface', 'can0')
        
        # 状态变量
        self.state = STATE_IDLE
        self.progress = 0
        self.error_code = ERROR_NONE
        self.extruder_num = 0  # 默认第一个挤出头
        
        # 初始化MCU对象
        mainsync = self.printer.lookup_object('mcu')._clocksync
        self._mcu = None
        self.cmd_queue = None
        self.send_id = None
        self.receive_id = None
        self.nodeid = None
        
        # 注册CAN总线ID
        self.printer.load_object(config, 'canbus_ids')
        
        # 创建MCU配置部分 - 直接使用原始配置，不尝试修改
        self._mcu_config = config.getsection(self.name)
        # ConfigWrapper对象没有set方法，不能直接修改配置
        # canbus_uuid和canbus_interface将从_mcu_config中读取
        
        # 注册事件处理器
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:mcu_identify", self._handle_mcu_identify)
        
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
        try:
            # 验证canbus_uuid格式
            if not self.canbus_uuid:
                raise self.printer.config_error("Missing required canbus_uuid parameter")
            
            self.logger.info("Initializing CAN communication with UUID: %s, interface: %s", 
                             self.canbus_uuid, self.can_interface)
            
            try:
                # 尝试将canbus_uuid转换为16进制数，验证格式
                uuid_int = int(self.canbus_uuid, 16)
                if uuid_int < 0 or uuid_int > 0xffffffffffff:
                    raise ValueError("Invalid UUID format")
            except ValueError:
                raise self.printer.config_error(
                    "Invalid canbus_uuid format '%s'. Must be a valid hexadecimal string (e.g. F01000000601)" 
                    % (self.canbus_uuid,))
            
            # 创建MCU对象
            from mcu import MCU, MCU_trsync
            from clocksync import SecondarySync
            import configparser
            
            # 获取CAN总线节点ID
            cbid = self.printer.lookup_object('canbus_ids')
            try:
                # 尝试获取已存在的节点ID
                self.nodeid = cbid.get_nodeid(self.canbus_uuid)
                self.logger.info("Using existing CAN node ID: %d", self.nodeid)
            except self.printer.config_error:
                # 如果不存在，则添加新的UUID并获取节点ID
                self.logger.info("Adding new UUID to canbus_ids with interface: %s", self.can_interface)
                # 创建一个临时配置对象用于添加UUID
                from configfile import ConfigWrapper
                import configparser
                temp_config_parser = configparser.ConfigParser()
                temp_section = "temp_section"
                temp_config_parser.add_section(temp_section)
                temp_config = ConfigWrapper(self.printer, temp_config_parser, {}, temp_section)
                
                self.nodeid = cbid.add_uuid(temp_config, self.canbus_uuid, self.can_interface)
                self.logger.info("Assigned new CAN node ID: %d", self.nodeid)
            
            # 创建一个新的配置对象，包含必要的CAN总线参数
            # ConfigWrapper对象没有set方法，所以我们需要创建一个新的配置
            config_parser = configparser.ConfigParser()
            section_name = "mcu " + self.name
            if not config_parser.has_section(section_name):
                config_parser.add_section(section_name)
            
            # 从原始配置复制所有参数
            for option in self._mcu_config.get_prefix_options(''):
                value = self._mcu_config.get(option)
                config_parser.set(section_name, option, value)
            
            # 设置CAN总线参数 - 确保使用正确的接口名称
            config_parser.set(section_name, 'canbus_uuid', self.canbus_uuid)
            config_parser.set(section_name, 'canbus_interface', self.can_interface)
            
            self.logger.info("Created config with section: %s, canbus_interface: %s", 
                             section_name, self.can_interface)
            
            # 创建新的ConfigWrapper对象
            from configfile import ConfigWrapper
            new_config = ConfigWrapper(self.printer, config_parser, {}, section_name)
            
            # 使用新配置创建MCU对象
            mainsync = self.printer.lookup_object('mcu')._clocksync
            self._mcu = MCU(new_config, SecondarySync(self.reactor, mainsync))
            self.printer.add_object(section_name, self._mcu)
            self.cmd_queue = self._mcu.alloc_command_queue()
            
            # 注册MCU响应处理函数
            self._mcu.register_config_callback(self._build_config)
            self._mcu.register_response(self._handle_cabinet_response, "cabinet_response")
            
            self.logger.info("FeederCabinet MCU initialized with interface: %s", self.can_interface)
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
            
    def _handle_mcu_identify(self):
        # 获取MCU常量
        constants = self._mcu.get_constants()
        self.logger.info("FeederCabinet MCU identified")
        
    def _build_config(self):
        # 创建命令
        self.cabinet_send_cmd = self._mcu.lookup_command(
            "cabinet_send cmd=%c extruder=%c",
            cq=self.cmd_queue
        )
        self.logger.info("FeederCabinet commands configured")
        
    def _handle_cabinet_response(self, params):
        # 处理从送料柜接收到的消息
        if len(params) < 3:
            self.logger.error("Received invalid message (too short)")
            return
        
        status = params['status']
        progress = params['progress']
        error_code = params['error']
        
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
    
    def _handle_ready(self):
        # 打印机就绪时设置为空闲状态
        self.state = STATE_IDLE
        self.progress = 0
        self.error_code = ERROR_NONE
        self.logger.info("FeederCabinet ready")
    
    def send_message(self, cmd_type, extruder_num=0):
        # 发送CAN消息到送料柜
        try:
            # 检查MCU是否已初始化
            if self._mcu is None:
                self.logger.error("Cannot send message: MCU not initialized")
                return False
            
            # 使用cabinet_send_cmd命令发送消息
            self.cabinet_send_cmd.send([cmd_type, extruder_num])
            self.logger.debug("Sent message: cmd=%d, extruder=%d", cmd_type, extruder_num)
            return True
        except Exception as e:
            self.logger.error("Failed to send message: %s", str(e))
            return False
    
    # 已移至_handle_cabinet_response方法
    
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