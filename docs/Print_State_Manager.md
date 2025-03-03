# 打印状态管理器

打印状态管理器是一个用于实现断电续打功能的Klipper模块。它可以定期保存打印状态，并在打印机断电后恢复打印。

## 配置

在`printer.cfg`中添加以下配置：

```ini
[print_state_manager]
# 自动保存间隔（秒），默认为10秒
auto_save_interval: 10
# 状态文件保存路径（推荐保存在SD卡上）
state_file: /path/to/print_state.json
```

## 工作原理

打印状态管理器通过以下机制保存打印状态：

1. **定时保存位置信息**
   - 每隔设定的时间间隔（默认10秒）保存当前打印位置
   - 包含文件位置、进度等基本信息
   - 这种保存方式开销小，适合频繁保存

2. **完整状态保存**
   - 在以下情况下保存完整打印状态：
     * 层变化时
     * 切换喷头时
     * 打印开始时
     * 打印暂停时
     * 打印完成时
     * 打印出错时
     * 手动触发保存时
   - 包含所有打印参数，如：
     * 温度设置
     * 风扇速度
     * 运动参数
     * 耗材使用情况

## 状态文件格式

状态文件使用JSON格式存储，包含以下主要信息：

```json
{
    "version": 1,
    "created_at": 1234567890,
    "printer_info": {
        "klipper_version": "v0.xx.x",
        "printer_name": "My Printer"
    },
    "position_state": {
        "timestamp": 1234567890,
        "filename": "my_print.gcode",
        "gcode_position": [x, y, z, e],
        "file_position": 1234,
        "progress": 0.5,
        "print_duration": 1800
    },
    "temperature_state": {
        "extruder0": {
            "target": 200,
            "current": 198,
            "pressure_advance": 0.5
        },
        "extruder1": {
            "target": 200,
            "current": 199,
            "pressure_advance": 0.5
        },
        "bed": {
            "target": 60,
            "current": 59
        }
    },
    "motion_state": {
        "current_position": [x, y, z, e],
        "active_extruder": 0,
        "speed": 100,
        "speed_factor": 1.0,
        "extrude_factor": 1.0
    },
    "cooling_state": {
        "part_fan_speed": {
            "t0": 1.0,
            "t1": 1.0
        },
        "heatsink_fan_speed": {
            "t0": 1.0,
            "t1": 1.0
        }
    },
    "filament_state": {
        "extruder0": {
            "total_extruded": 100.5,
            "material": "PLA"
        },
        "extruder1": {
            "total_extruded": 50.2,
            "material": "PLA"
        }
    }
}
```

## 可用命令

### SAVE_PRINT_STATE

手动保存当前打印状态。这将保存完整的打印状态。

用法：
```gcode
SAVE_PRINT_STATE
```

## 注意事项

1. **存储位置**
   - 建议将状态文件保存在SD卡或其他非易失性存储设备上
   - 确保存储位置有足够的空间
   - 路径使用正斜杠'/'，即使在Windows系统上也是如此

2. **性能考虑**
   - 轻量级位置保存（每N秒）对性能影响很小
   - 完整状态保存可能会导致短暂的停顿
   - 可以通过调整auto_save_interval来平衡保存频率和性能

3. **安全性**
   - 使用原子性写入确保状态文件完整性
   - 发生错误时会保留上一个有效的状态文件
   - 建议定期备份状态文件

4. **双喷头支持**
   - 自动检测并支持双喷头配置
   - 分别记录两个喷头的所有参数
   - 支持复制/镜像打印模式

## 故障排除

1. **状态文件无法保存**
   - 检查存储设备是否有写入权限
   - 确保存储路径存在
   - 检查存储设备是否有足够空间

2. **状态文件损坏**
   - 程序会自动使用临时文件进行原子性写入
   - 如果发现损坏，将使用上一个有效的状态文件
   - 建议定期检查状态文件的完整性

3. **性能问题**
   - 如果发现明显的停顿，可以增加auto_save_interval的值
   - 确保存储设备的写入速度足够快
   - 考虑使用更快的存储设备

## 未来改进计划

1. 添加状态文件压缩功能
2. 支持多个备份状态文件
3. 添加状态恢复时的预热等待功能
4. 支持更多打印参数的保存和恢复
5. 添加Web界面支持 