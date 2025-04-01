# 送料柜自动续料模块
#
# 基于CAN总线通信，实现与外部送料柜的通信和控制
#
# Copyright (C) 2023  Your Name <your.email@example.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import time

class FeederCabinet:
    # 状态常量定义
    STATE_IDLE = 'idle'          # 空闲状态
    STATE_RUNOUT = 'runout'      # 耗材用尽
    STATE_REQUESTING = 'requesting'  # 请求送料
    STATE_FEEDING = 'feeding'    # 送料中
    STATE_LOADED = 'loaded'      # 送料完成
    STATE_ERROR = 'error'        # 错误状态
    STATE_COMPLETE = 'complete'  # 完成状态
    
    # 命令类型定义
    CMD_REQUEST_FEED = 0x01      # 请求补料
    CMD_STOP_FEED = 0x02         # 停止补料
    CMD_QUERY_STATUS = 0x03      # 状态查询
    CMD_PRINTING = 0x04          # 打印中
    CMD_PRINT_COMPLETE = 0x05    # 打印完成
    CMD_PRINT_PAUSE = 0x06       # 打印暂停
    CMD_PRINT_CANCEL = 0x07      # 打印取消
    CMD_PRINTER_IDLE = 0x08      # 打印机空闲
    
    # 错误码定义
    ERROR_NONE = 0x00            # 无错误
    ERROR_MECHANICAL = 0x01      # 机械故障
    ERROR_NO_FILAMENT = 0x02     # 耗材缺失
    ERROR_OTHER = 0x03           # 其他错误
    
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        
        # 获取CAN总线配置
        self.canbus_uuid = config.get('canbus_uuid')
        self.can_interface = config.get('canbus_interface', 'can0')
        self.can_node_id = None  # 节点ID，初始化时获取
        self.tx_id = None        # 发送ID
        self.rx_id = None        # 接收ID
        self.mcu = None          # MCU对象
        self.cmd_queue = None    # 命令队列
        self.send_command_cmd = None  # 发送命令函数
        
        # 状态变量
        self.state = self.STATE_IDLE
        self.progress = 0        # 进度 (0-100)
        self.error_code = self.ERROR_NONE
        self.retry_count = 0     # 重试计数
        self.max_retries = 3     # 最大重试次数
        self.last_status_time = 0  # 最后状态更新时间
        self.status_timeout = 10.0  # 状态超时时间（秒）
        
        # 注册事件处理器
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        
        # 注册G-code命令
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('START_FEEDER_CABINET', self.cmd_START_FEEDER_CABINET,
                               desc="手动启动续料")
        gcode.register_command('QUERY_FEEDER_CABINET', self.cmd_QUERY_FEEDER_CABINET,
                               desc="查询当前状态")
        gcode.register_command('CANCEL_FEEDER_CABINET', self.cmd_CANCEL_FEEDER_CABINET,
                               desc="取消续料操作")
        
        # 注册定时任务
        self.status_timer = self.reactor.register_timer(self._status_timer, self.reactor.NOW)
    
    def _log(self, level, msg):
        """记录日志"""
        if level == 0:
            logging.error("feeder_cabinet: %s", msg)
        elif level == 1:
            logging.info("feeder_cabinet: %s", msg)
        else:
            logging.debug("feeder_cabinet: %s", msg)
    
    def _handle_connect(self):
        """连接时初始化CAN总线"""
        self._init_canbus()
        self._log(1, "送料柜模块初始化完成")
        # 发送初始状态查询
        self._query_status()
    
    def _handle_disconnect(self):
        """断开连接时清理资源"""
        if self.status_timer is not None:
            self.reactor.unregister_timer(self.status_timer)
            self.status_timer = None
        self._log(1, "送料柜模块已断开")
    
    def _send_can_message(self, cmd, data=None):
        """发送CAN消息"""
        msg = bytearray([cmd])  # 命令类型
        msg.append(0)  # 挤出头编号(默认0)
        
        # 添加数据
        if data is not None:
            if isinstance(data, (list, bytearray)):
                msg.extend(data)
            else:
                msg.append(data)
        
        # 补齐消息长度到8字节
        while len(msg) < 8:
            msg.append(0)
        
        self._log(2, "发送CAN消息: 命令=%d, 数据=%s" % (cmd, list(msg)))
        
        try:
            # 发送命令并等待响应
            params = self.send_command_cmd.send([cmd, msg])
            if params is not None and 'resp' in params:
                self._handle_can_receive(params['resp'])
        except Exception as e:
            self._log(0, "发送CAN消息失败: %s" % str(e))
    
    def _handle_can_receive(self, data):
        """处理接收到的CAN消息"""
        if not data or len(data) < 3:
            self._log(0, "接收到无效的CAN消息")
            return
        
        # 解析消息内容
        try:
            status = data[0]  # 状态码
            progress = data[1]  # 进度
            error = data[2]  # 错误码
            
            self._log(2, "接收CAN消息: 状态=%d, 进度=%d, 错误=%d" % 
                     (status, progress, error))
            
            # 更新状态
            self.progress = progress
            self.error_code = error
            
            # 根据状态码更新状态
            if status == 0x00:  # 空闲
                self.state = self.STATE_IDLE
                self.retry_count = 0  # 重置重试计数
            elif status == 0x01:  # 准备送料
                self.state = self.STATE_REQUESTING
            elif status == 0x02:  # 送料中
                self.state = self.STATE_FEEDING
            elif status == 0x03:  # 送料完成
                self.state = self.STATE_LOADED
                self.retry_count = 0  # 重置重试计数
                self._handle_load_complete()
            elif status == 0x04:  # 送料失败
                self._handle_error(error)
            
            # 记录最后接收状态的时间
            self.last_status_time = self.reactor.monotonic()
        except Exception as e:
            self._log(0, "处理CAN消息失败: %s" % str(e))
    
    def _init_canbus(self):
        """初始化CAN总线通信"""
        self._log(1, "开始初始化CAN总线通信")
        
        # 首先获取所有可用的CAN接口
        canbus_interfaces = {}
        for section in self.printer.get_config_sections():
            if not section.startswith('mcu '):
                continue
            mcu_config = self.printer.lookup_object('configfile').getsection(section)
            if mcu_config.get('canbus_interface', None) is None:
                continue
            mcu_name = section[4:] if section != 'mcu' else 'mcu'
            canbus_interface = mcu_config.get('canbus_interface')
            canbus_interfaces[canbus_interface] = mcu_name
        
        # 检查指定的CAN接口是否存在
        if self.can_interface not in canbus_interfaces:
            raise self.printer.config_error(
                "找不到CAN接口'%s'，请确保在[mcu]配置中定义了该接口" % (self.can_interface,))
        
        # 获取CAN节点ID
        try:
            self._log(2, "尝试获取UUID为'%s'的节点ID" % self.canbus_uuid)
            canbus_ids = self.printer.load_object(None, 'canbus_ids')
            self.can_node_id = canbus_ids.get_nodeid(self.canbus_uuid)
            self._log(1, "成功获取节点ID: %d" % self.can_node_id)
        except Exception as e:
            self._log(0, "获取节点ID失败: %s" % str(e))
            msg = "找不到UUID为'%s'的CAN设备\n" % (self.canbus_uuid,)
            msg += "请确保设备已正确连接并运行以下命令检查可用设备：\n"
            msg += "~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py %s" % (
                self.can_interface,)
            raise self.printer.config_error(msg)
        
        # 获取MCU对象
        try:
            mcu_name = canbus_interfaces[self.can_interface]
            if mcu_name == 'mcu':
                self.mcu = self.printer.lookup_object('mcu')
            else:
                self.mcu = self.printer.lookup_object('mcu ' + mcu_name)
            self._log(1, "成功获取MCU对象: %s" % mcu_name)
        except Exception as e:
            self._log(0, "获取MCU对象失败: %s" % str(e))
            raise self.printer.config_error(
                "无法获取与CAN接口'%s'关联的MCU对象" % (self.can_interface,))
        
        # 设置发送和接收ID
        self.tx_id = self.can_node_id * 2 + 256
        self.rx_id = self.tx_id + 1
        self._log(2, "设置CAN ID - 发送: %d, 接收: %d" % (self.tx_id, self.rx_id))
        
        # 注册命令和回调
        self.cmd_queue = self.mcu.alloc_command_queue()
        self.mcu.register_response(self._handle_can_receive, 'feeder_response')
        
        # 定义发送命令的函数
        self.send_command_cmd = self.mcu.lookup_query_command(
            "feeder_send_command cmd=%c data=%*s",
            "feeder_response resp=%*s",
            self.cmd_queue)
        
        self._log(1, "CAN总线初始化完成")
    
    def _status_timer(self, eventtime):
        """定时状态检查"""
        # 检查是否需要重新查询状态
        if self.state not in [self.STATE_IDLE, self.STATE_COMPLETE]:
            current_time = self.reactor.monotonic()
            if current_time - self.last_status_time > self.status_timeout:
                self._log(1, "状态更新超时，重新查询状态")
                self._query_status()
        
        # 每5秒执行一次
        return eventtime + 5.0
    
    def _query_status(self):
        """查询当前状态"""
        self._send_can_message(self.CMD_QUERY_STATUS)
    
    def _request_feed(self):
        """请求送料"""
        if self.state not in [self.STATE_IDLE, self.STATE_RUNOUT]:
            self._log(0, "当前状态 '%s' 不允许请求送料" % self.state)
            return False
        
        self._log(1, "发送送料请求")
        self.state = self.STATE_REQUESTING
        self._send_can_message(self.CMD_REQUEST_FEED)
        return True
    
    def _stop_feed(self):
        """停止送料"""
        if self.state not in [self.STATE_REQUESTING, self.STATE_FEEDING]:
            self._log(0, "当前状态 '%s' 不允许停止送料" % self.state)
            return False
        
        self._log(1, "发送停止送料请求")
        self._send_can_message(self.CMD_STOP_FEED)
        return True
    
    def _handle_load_complete(self):
        """处理送料完成"""
        self._log(1, "送料完成")
        self.state = self.STATE_COMPLETE
        # 通知其他模块送料已完成，可以恢复打印
        self.printer.send_event("feeder_cabinet:feed_complete")
        # 延迟一段时间后切换回IDLE状态
        self.reactor.register_callback(
            lambda e: self._set_state(self.STATE_IDLE), self.reactor.monotonic() + 5.0)
    
    def _handle_error(self, error_code):
        """处理错误"""
        error_msg = "未知错误"
        if error_code == self.ERROR_MECHANICAL:
            error_msg = "机械故障"
        elif error_code == self.ERROR_NO_FILAMENT:
            error_msg = "耗材缺失"
        elif error_code == self.ERROR_OTHER:
            error_msg = "其他错误"
        
        self._log(0, "送料错误: %s (错误码: %d)" % (error_msg, error_code))
        self.state = self.STATE_ERROR
        
        # 如果错误次数未超过最大重试次数，尝试重新发送请求
        if self.retry_count < self.max_retries:
            self.retry_count += 1
            self._log(1, "尝试重新送料 (第 %d 次重试)" % self.retry_count)
            self.reactor.register_callback(
                lambda e: self._request_feed(), self.reactor.monotonic() + 2.0)
        else:
            self._log(0, "超过最大重试次数，放弃送料")
            # 通知其他模块送料失败
            self.printer.send_event("feeder_cabinet:feed_failed")
    
    def _set_state(self, state):
        """设置状态"""
        if self.state == state:
            return
        self._log(2, "状态变更: %s -> %s" % (self.state, state))
        self.state = state
    
    def notify_runout(self):
        """通知耗材用尽"""
        if self.state != self.STATE_IDLE:
            self._log(0, "当前状态 '%s' 不允许处理耗材用尽事件" % self.state)
            return False
        
        self._log(1, "检测到耗材用尽，准备请求送料")
        self.state = self.STATE_RUNOUT
        return self._request_feed()
    
    def notify_pause(self):
        """通知打印暂停"""
        self._log(1, "打印暂停")
        self._send_can_message(self.CMD_PRINT_PAUSE)
    
    def notify_resume(self):
        """通知打印恢复"""
        self._log(1, "打印恢复")
        self._send_can_message(self.CMD_PRINTING)
    
    def notify_cancel(self):
        """通知打印取消"""
        self._log(1, "打印取消")
        self._send_can_message(self.CMD_PRINT_CANCEL)
    
    def notify_complete(self):
        """通知打印完成"""
        self._log(1, "打印完成")
        self._send_can_message(self.CMD_PRINT_COMPLETE)
    
    # G-code命令处理函数
    def cmd_START_FEEDER_CABINET(self, gcmd):
        """G-code命令: START_FEEDER_CABINET - 手动启动续料"""
        if self._request_feed():
            gcmd.respond_info("已发送续料请求")
        else:
            gcmd.respond_info("无法发送续料请求，当前状态: %s" % self.state)
    
    def cmd_QUERY_FEEDER_CABINET(self, gcmd):
        """G-code命令: QUERY_FEEDER_CABINET - 查询当前状态"""
        self._query_status()
        
        # 准备状态信息
        status_text = "未知"
        if self.state == self.STATE_IDLE:
            status_text = "空闲"
        elif self.state == self.STATE_RUNOUT:
            status_text = "耗材用尽"
        elif self.state == self.STATE_REQUESTING:
            status_text = "请求送料中"
        elif self.state == self.STATE_FEEDING:
            status_text = "送料中"
        elif self.state == self.STATE_LOADED:
            status_text = "送料完成"
        elif self.state == self.STATE_COMPLETE:
            status_text = "操作完成"
        elif self.state == self.STATE_ERROR:
            status_text = "错误"
        
        error_text = "无"
        if self.error_code == self.ERROR_MECHANICAL:
            error_text = "机械故障"
        elif self.error_code == self.ERROR_NO_FILAMENT:
            error_text = "耗材缺失"
        elif self.error_code == self.ERROR_OTHER:
            error_text = "其他错误"
        
        # 返回状态信息
        gcmd.respond_info(
            "当前状态：%s\n"
            "进度：%d%%\n"
            "错误信息：%s" % (status_text, self.progress, error_text))
    
    def cmd_CANCEL_FEEDER_CABINET(self, gcmd):
        """G-code命令: CANCEL_FEEDER_CABINET - 取消续料操作"""
        if self._stop_feed():
            gcmd.respond_info("已发送取消续料请求")
        else:
            gcmd.respond_info("无法取消续料，当前状态: %s" % self.state)

def load_config(config):
    return FeederCabinet(config)
