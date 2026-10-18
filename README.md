# serial-proto-lens

[![tests](https://github.com/dunboy/serial-proto-lens/actions/workflows/tests.yml/badge.svg)](https://github.com/dunboy/serial-proto-lens/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> 从一个未知的串口抓包里，推断出帧结构、字段含义和校验算法。
>
> Infer frame boundaries, field roles and checksum algorithms from a raw serial capture.
> Zero dependencies, Python 3.9+.

---

## 它解决什么问题

你手里有个模块、传感器或者串口屏。厂家给了个"协议文档"，但里面：

- 只写了几个示例帧，没写完整帧格式
- 校验算法写的是"校验和"，但没说校验哪几个字节
- 某些字段的含义完全没提

于是你只能对着逻辑分析仪或者串口助手的十六进制数据流发呆。

这个工具做三件事：

1. **找帧边界** —— 帧头在哪，一帧多长
2. **拆字段** —— 哪些字节是常量（帧头、地址、长度），哪些在变（数据），哪些在递增（计数器）
3. **认校验** —— 末尾那个字节是什么算法算出来的

## 快速开始

不需要安装任何东西，克隆下来直接用：

```bash
git clone https://github.com/dunboy/serial-proto-lens.git
cd serial-proto-lens

python serial_proto_lens.py report examples/sample_capture.hex
```

只需要 Python 3.9 或更高版本，**没有任何第三方依赖**。

### 三种用法

```bash
# 一次跑完三项分析
python serial_proto_lens.py report capture.hex

# 只把帧切出来看（已知帧长时）
python serial_proto_lens.py frames capture.hex --length 9 --limit 20

# 只测校验算法
python serial_proto_lens.py checksum capture.hex --length 9

# 帧头检测不准时，手动指定
python serial_proto_lens.py frames capture.hex --length 9 --header AA55
```

## 输出长什么样

对仓库里那份合成抓包（120 帧、9 字节协议）跑出来的真实结果：

```
serial-proto-lens
file   examples/sample_capture.hex
bytes  1080

[1] frame boundaries
  * header AA 55  frame length  9 B · 100.0% of gaps · ~120 frames

[2] field map · 120 frames × 9 B
  off  values              distinct  note
    0  AA                         1  constant AA
    1  55                         1  constant 55
    2  04                         1  constant 04
    3  01 02 03 04 05             5  varying
    4  C8 CB CE D1 D4 D7, ...      120  counter, step +3
    5  00 01 02                   3  varying
    6  00 01 02 03 04 05, ...      120  counter, step +1
    7  00                         1  constant 00
    8  CD D2 D7 DC E1 E6, ...       68  varying

  grouped fields
    bytes 0-2    constant AA 55 04
    byte 3       varying
    byte 4       counter, step +3
    byte 5       varying
    byte 6       counter, step +1
    byte 7       constant 00
    byte 8       varying

[3] checksum (trailing field)
  * sum8               over skipping first 2 bytes       100.0% match
```

读法：帧头是 `AA 55`，帧长 9 字节；第 3 字节是命令码（5 种取值）；第 4 字节是步长为 3 的递增数据；第 6 字节是步长为 1 的计数器；**最后一字节是前 6 个字节的 8 位累加和**。

这三项结论，人工对着数据流看大概要半小时，工具一秒给出来。

## 支持的输入

| 格式 | 说明 |
|---|---|
| `AA 55 01` | 空格分隔的十六进制 |
| `AA,55,01` | 逗号分隔 |
| `0xAA 0x55` | 带前缀 |
| `AA5501` | 连续十六进制串 |
| `AA 55  # 帧头` | 带注释（`#` 和 `//` 之后会被忽略） |
| `.bin` 等二进制文件 | 直接按原始字节读取 |

后缀是 `.hex` / `.txt` / `.log` / `.csv` 的文件按十六进制文本解析，解析不出内容时回退为原始字节。

## 原理

三个分析各自独立，都是启发式的：

**帧边界** —— 对每个 1 字节和 2 字节序列，记录它在数据流里出现的位置，然后看"相邻两次出现的间距"是否集中在同一个值上。真正的帧头每次间隔固定，负载字节不会。得到候选帧长后，还会剔除谐波（比如 9 字节协议的 45 字节周期只是 3 帧）。

**字段分析** —— 按帧长切分后，对每个字节偏移统计取值分布：只有一种取值 = 常量；相邻帧差值恒定为非零常数 = 计数器；其余 = 变化字段。相邻的同类字节会合并成一个字段。

**校验识别** —— 假设校验值在帧尾，枚举 5 种常见算法（`sum8`、`xor8`、`crc8/0x07`、`crc16/modbus`、`crc16/ccitt-false`）× 3 种覆盖范围（整帧、跳过首字节、跳过前两字节）× 16 位算法的大小端，命中率 ≥90% 才报告。

## 局限

**这是个启发式工具，不是协议解析器。** 说清楚它做不到什么：

- **校验值必须在帧尾。** 放在帧头之后的协议识别不了。
- **只认 5 种常见算法。** 自定义查表 CRC、加盐校验、滚动校验都识别不出。
- **变长帧处理不了。** 帧长字段驱动的协议需要先按帧长字段自己切好。
- **至少需要 4 帧**才能给出候选，帧数越多结论越可靠。
- **帧头必须真的重复。** 如果帧头本身带随机数或者被加密，检测会失败。
- **字段语义靠猜。** 工具只能告诉你"这个字节在递增"，不能告诉你"这是温度"。语义还得靠你结合业务判断。

## 测试

```bash
python -m unittest discover -s tests -v
```

26 个测试，覆盖十六进制解析、CRC 标准校验值（`"123456789"` 的已知结果）、帧检测、谐波剔除、字段分类、校验识别、报告渲染。

`examples/make_sample.py` 可以重新生成那份合成抓包。

## 协议

MIT
