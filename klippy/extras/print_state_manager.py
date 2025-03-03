import os
import json
import time
import logging
import traceback

class PrintStateManager:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        
        # 配置参数
        self.auto_save_interval = config.getint('auto_save_interval', 10)
        self.state_file = os.path.expanduser(config.get('state_file', '~/print_state.json'))
        logging.info(f"打印状态管理器初始化: 保存间隔={self.auto_save_interval}秒, 状态文件={self.state_file}")
        
        # 内部状态
        self.is_printing = False
        self.last_save_time = 0
        self.last_layer = 0
        self.last_z_pos = 0.0
        self._auto_save_timer = None
        
        # 组件引用（将在connect时初始化）
        self.print_stats = None
        self.toolhead = None
        self.gcode_move = None
        self.gcode = None
        self.virtual_sdcard = None
        
        # 注册事件处理器
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("idle_timeout:printing", self._handle_printing)
        self.printer.register_event_handler("idle_timeout:ready", self._handle_print_complete)
        self.printer.register_event_handler("idle_timeout:idle", self._handle_print_complete)
        self.printer.register_event_handler("virtual_sdcard:reset_file", self._handle_print_error)
        
        # 初始化状态轮询
        self.last_print_state = None
        self._state_check_timer = None
        
        # 确保状态文件目录存在
        state_dir = os.path.dirname(self.state_file)
        if state_dir:
            try:
                os.makedirs(state_dir, exist_ok=True)
                logging.info(f"确保状态文件目录存在: {state_dir}")
            except Exception as e:
                logging.error(f"创建状态文件目录失败: {str(e)}")
    
    def _handle_connect(self):
        """初始化组件连接"""
        try:
            self.print_stats = self.printer.lookup_object('print_stats')
            if self.print_stats is None:
                logging.error("无法获取print_stats组件")
                return
            logging.info("成功获取print_stats组件")
            
            self.virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
            if self.virtual_sdcard is None:
                logging.error("无法获取virtual_sdcard组件")
                return
            logging.info("成功获取virtual_sdcard组件")
            
            self.toolhead = self.printer.lookup_object('toolhead')
            self.gcode_move = self.printer.lookup_object('gcode_move')
            self.gcode = self.printer.lookup_object('gcode')
            logging.info("打印状态管理器组件初始化成功")
            
            # 注册G代码命令
            self.gcode.register_command(
                'SAVE_PRINT_STATE', self.cmd_SAVE_PRINT_STATE,
                desc="手动保存当前打印状态"
            )
            
            # 测试状态文件写入权限
            self._test_file_permissions()
            
        except Exception as e:
            logging.error(f"打印状态管理器初始化失败: {str(e)}")
    
    def _test_file_permissions(self):
        """测试状态文件的读写权限"""
        try:
            # 尝试写入测试状态
            test_state = self._create_initial_state()
            self._atomic_save(test_state)
            logging.info("状态文件写入测试成功")
        except Exception as e:
            logging.error(f"状态文件写入测试失败: {str(e)}")
    
    def _start_state_check_timer(self):
        """启动状态检查定时器"""
        if self._state_check_timer is not None:
            return
        self._state_check_timer = self.reactor.register_timer(
            self._check_print_state,
            self.reactor.NOW + 1.0
        )
        logging.info("状态检查定时器已启动")
    
    def _check_print_state(self, eventtime):
        """检查打印状态变化"""
        if self.print_stats is None:
            return self.reactor.NEVER
            
        try:
            current_state = self.print_stats.get_status(eventtime)['state']
            
            if self.last_print_state != current_state:
                logging.info(f"检测到打印状态变化: {self.last_print_state} -> {current_state}")
                
                if current_state == 'printing':
                    self._handle_printing(eventtime)
                elif current_state == 'complete':
                    self._handle_print_complete(eventtime)
                elif current_state == 'error':
                    error_msg = self.print_stats.get_status(eventtime).get('message', '')
                    self._handle_print_error(eventtime)
                elif current_state == 'paused':
                    self._handle_print_paused(eventtime)
                    
                self.last_print_state = current_state
                
        except Exception as e:
            logging.error(f"检查打印状态失败: {str(e)}")
            
        return eventtime + 1.0  # 每秒检查一次状态
    
    def _handle_printing(self, print_time):
        """打印开始处理"""
        try:
            logging.info("收到打印开始事件")
            self.is_printing = True
            self.last_layer = 0
            self.last_z_pos = 0.0
            self.last_save_time = time.time()
            self._start_auto_save_timer()
            self._save_complete_state()
            logging.info(f"打印开始处理完成: is_printing={self.is_printing}")
        except Exception as e:
            logging.error(f"处理打印开始事件失败: {str(e)}")
    
    def _handle_print_complete(self, print_time):
        """打印完成处理"""
        self._save_complete_state()
        self.is_printing = False
        logging.info("打印完成，已保存最终状态")
    
    def _handle_print_error(self, print_time):
        """打印错误处理"""
        self._save_complete_state()
        self.is_printing = False
        logging.info("打印出错，已保存状态")
    
    def _handle_print_paused(self, print_time):
        """打印暂停处理"""
        self._save_complete_state()
        logging.info("打印暂停，已保存状态")
    
    def _check_layer_change(self):
        """检查是否发生层变化"""
        if not self.is_printing:
            return False
            
        try:
            current_z = self.toolhead.get_position()[2]
            if abs(current_z - self.last_z_pos) >= 0.1:  # 层高阈值设为0.1mm
                logging.info(f"检测到层变化: 上一层Z={self.last_z_pos:.3f}, 当前Z={current_z:.3f}")
                self.last_z_pos = current_z
                self.last_layer += 1
                return True
        except Exception as e:
            logging.error(f"检查层变化失败: {str(e)}")
        return False
    
    def _start_auto_save_timer(self):
        """启动定时保存任务"""
        try:
            if self._auto_save_timer is not None:
                return
            self._auto_save_timer = self.reactor.register_timer(
                self._auto_save_callback,
                self.reactor.NOW + 1.0  # 立即开始第一次检查
            )
            logging.info("自动保存定时器已启动")
        except Exception as e:
            logging.error(f"启动自动保存定时器失败: {str(e)}")
    
    def _auto_save_callback(self, eventtime):
        """定时保存回调函数"""
        if not self.is_printing:
            self._auto_save_timer = None
            logging.info("打印已停止，关闭自动保存定时器")
            return self.reactor.NEVER
        
        try:
            current_time = time.time()
            
            # 检查是否需要保存完整状态
            if self._check_layer_change():
                logging.info("检测到层变化，触发状态保存")
                self._save_complete_state()
                self.last_save_time = current_time
            elif (current_time - self.last_save_time) >= self.auto_save_interval:
                logging.info("达到自动保存间隔，触发状态保存")
                self._save_complete_state()
                self.last_save_time = current_time
            
        except Exception as e:
            logging.error(f"自动保存回调执行失败: {str(e)}")
        
        return eventtime + 1.0  # 每秒检查一次
    
    def _create_initial_state(self):
        """创建初始状态结构"""
        return {
            'version': 1,
            'created_at': time.time(),
            'printer_info': {
                'klipper_version': self.printer.get_start_args().get('software_version'),
                'printer_name': self.printer.get_start_args().get('printer_name')
            }
        }
    
    def _get_position_state(self):
        """获取位置状态"""
        status = self.print_stats.get_status()
        gcode_status = self.gcode_move.get_status()
        return {
            'timestamp': time.time(),
            'filename': status['filename'],
            'current_layer': self.last_layer,
            'current_z': self.last_z_pos,
            'gcode_position': gcode_status['gcode_position'],
            'absolute_coordinates': gcode_status['absolute_coordinates'],
            'absolute_extrude': gcode_status['absolute_extrude'],
            'file_position': status.get('file_position', 0),
            'progress': status['progress'],
            'print_duration': status['print_duration']
        }
    
    def _get_temperature_state(self):
        """获取温度状态"""
        extruder0 = self.printer.lookup_object('extruder0')
        extruder1 = self.printer.lookup_object('extruder1', None)
        heater_bed = self.printer.lookup_object('heater_bed')
        
        temp_state = {
            'extruder0': {
                'target': extruder0.get_status()['target'],
                'current': extruder0.get_status()['temperature'],
                'pressure_advance': extruder0.get_status().get('pressure_advance', 0)
            },
            'bed': {
                'target': heater_bed.get_status()['target'],
                'current': heater_bed.get_status()['temperature']
            }
        }
        
        # 检查是否存在第二个挤出机
        if extruder1 is not None:
            temp_state['extruder1'] = {
                'target': extruder1.get_status()['target'],
                'current': extruder1.get_status()['temperature'],
                'pressure_advance': extruder1.get_status().get('pressure_advance', 0)
            }
        
        return temp_state
    
    def _get_motion_state(self):
        """获取运动状态"""
        gcode_status = self.gcode_move.get_status()
        return {
            'current_position': self.toolhead.get_position(),
            'speed': gcode_status['speed'],
            'speed_factor': gcode_status['speed_factor'],
            'extrude_factor': gcode_status['extrude_factor']
        }
    
    def _get_cooling_state(self):
        """获取冷却状态"""
        cooling_state = {'part_fan_speed': {}, 'heatsink_fan_speed': {}}
        
        # 获取部件冷却风扇状态
        for i in range(2):  # 检查两个喷头的风扇
            fan_name = f'fan{i if i > 0 else ""}'
            fan = self.printer.lookup_object(fan_name, None)
            if fan is not None:
                cooling_state['part_fan_speed'][f't{i}'] = fan.get_status()['speed']
        
        # 获取散热器风扇状态
        for i in range(2):
            fan_name = f'heater_fan{i if i > 0 else ""}'
            fan = self.printer.lookup_object(fan_name, None)
            if fan is not None:
                cooling_state['heatsink_fan_speed'][f't{i}'] = fan.get_status()['speed']
        
        return cooling_state
    
    def _get_filament_state(self):
        """获取耗材状态"""
        status = self.print_stats.get_status()
        filament_state = {
            'extruder0': {
                'total_extruded': status['filament_used'],
                'material': 'unknown'
            }
        }
        
        # 检查第二个挤出机
        if 'filament_used1' in status:
            filament_state['extruder1'] = {
                'total_extruded': status['filament_used1'],
                'material': 'unknown'
            }
        
        return filament_state
    
    def _save_complete_state(self):
        """保存完整打印状态"""
        logging.info(f"尝试保存状态: is_printing={self.is_printing}")
        if not self.is_printing:
            logging.info("未在打印中，跳过状态保存")
            return
            
        try:
            # 获取当前状态
            new_state = {
                'timestamp': time.time(),
                'position_state': self._get_position_state(),
                'temperature_state': self._get_temperature_state(),
                'motion_state': self._get_motion_state(),
                'cooling_state': self._get_cooling_state(),
                'filament_state': self._get_filament_state()
            }
            
            logging.info(f"已收集新状态数据: {json.dumps(new_state, indent=2)}")
            
            # 读取或创建状态文件
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    logging.info("成功读取现有状态文件")
            except (FileNotFoundError, json.JSONDecodeError):
                logging.info("创建新的状态文件")
                state = self._create_initial_state()
            
            # 更新状态
            state.update(new_state)
            
            # 保存状态
            self._atomic_save(state)
            logging.info(f"状态保存成功: 层={self.last_layer}, Z={self.last_z_pos:.3f}, 进度={new_state['position_state']['progress']:.1%}")
            
        except Exception as e:
            logging.error(f"保存状态失败: {str(e)}, 详细错误: {traceback.format_exc()}")
    
    def _atomic_save(self, state):
        """原子性保存状态文件"""
        temp_file = self.state_file + '.temp'
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            
            # 写入临时文件
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            
            # 原子性替换文件
            os.replace(temp_file, self.state_file)
            
        except Exception as e:
            logging.error(f"原子性保存失败: {str(e)}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
            raise
    
    def cmd_SAVE_PRINT_STATE(self, gcmd):
        """手动保存状态的G代码命令"""
        self._save_complete_state()
        gcmd.respond_info("打印状态已保存")

def load_config(config):
    return PrintStateManager(config) 