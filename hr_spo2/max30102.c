/*
 * Copyright (C) 2021 Shenzhen Kaihong Digital Industry Development Co.,Ltd.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http:// www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 *
 * limitations under the License.
 */
#include <stdio.h>
#include <unistd.h>
#include <string.h>
#include <math.h>
#include <stdint.h>

#include "ohos_init.h"
#include "cmsis_os2.h"
#include "ohos_types.h"
#include "iot_errno.h"
#include "iot_gpio.h"
#include "iot_i2c.h"
#include "max30102.h"
#include "algorithm.h"

#define MAX30102_TASK_STACK_SIZE    4096            // 任务栈大小
#define MAX30102_TASK_PRIO          24              // 任务优先等级
#define MAX30102_I2C_IDX            (1)             // 0:I2C设备号, 15: I2C SDA对应引脚, 02: I2C SCL对应引脚
#define MAX30102_I2C_BAUDRATE       (100 * 1000)    // I2C波特率
#define MAX30102_I2C_SDA            15              // I2C0 SDA对应引脚
#define MAX30102_I2C_SCL            2               // I2C0 SCL对应引脚
#define MAX30102_ADDR               0x57            // MAX30102设备地址

#define MAX30102_INT_GPIO           0              // MAX30102中断引脚

// ============================================================================
// PPG 信号滤波参数（中值滤波 + 指数移动平均）
// ============================================================================
#define MEDIAN_WINDOW_SIZE          5               // 中值滤波窗口（去除手指移动尖峰）
#define EMA_ALPHA                   0.3f            // EMA 平滑系数（0~1，越小越平滑）

// PPG 信号滤波器结构体
typedef struct {
    uint32_t buf[MEDIAN_WINDOW_SIZE];               // 中值滤波环形缓冲
    uint32_t idx;                                   // 当前写入位置
    float ema_output;                               // EMA 输出值
    bool initialized;                               // 是否已初始化
} PpgFilter_t;

// ============================================================================
// 输出级滤波参数（信号质量检测 + 历史值平滑 + 异常拒绝）
// ============================================================================
#define OUTPUT_HISTORY_SIZE         5               // 输出历史缓冲大小
#define HR_MAX_CHANGE_PER_CYCLE     15              // 心率每次最大允许变化量(bpm)
#define SPO2_MAX_CHANGE_PER_CYCLE   8               // 血氧每次最大允许变化量(%)
#define HR_VALID_MIN                40              // 有效心率最小值
#define HR_VALID_MAX                160             // 有效心率最大值
#define SPO2_VALID_MIN              70              // 有效血氧最小值（低于此值视为异常）
#define SPO2_VALID_MAX              100             // 有效血氧最大值
#define SQI_VARIANCE_THRESHOLD      500000000ULL    // 信号质量方差阈值（超过此值认为信号差）
#define INVALID_COUNT_THRESHOLD     5               // 连续无效次数阈值，超过则重置

// 输出级滤波器
typedef struct {
    uint8_t hr_history[OUTPUT_HISTORY_SIZE];
    uint8_t spo2_history[OUTPUT_HISTORY_SIZE];
    uint8_t history_count;                          // 历史有效样本数
    uint8_t history_idx;                            // 写入位置
    uint8_t last_valid_hr;                          // 上次有效心率
    uint8_t last_valid_spo2;                        // 上次有效血氧
    uint8_t invalid_count;                          // 连续无效计数
    bool has_valid_output;                          // 是否已有有效输出
} OutputFilter_t;

static OutputFilter_t g_output_filter = {0};

static bool finger = false;
#define DEF_TIMEOUT (5 * 10000) // 超时时间5s

static osThreadId_t g_tid = NULL;
osSemaphoreId_t g_hrso2_sensor_sem = NULL;

// register addresses
#define REG_INTR_STATUS_1   0x00
#define REG_INTR_STATUS_2   0x01
#define REG_INTR_ENABLE_1   0x02
#define REG_INTR_ENABLE_2   0x03
#define REG_FIFO_WR_PTR     0x04
#define REG_OVF_COUNTER     0x05
#define REG_FIFO_RD_PTR     0x06
#define REG_FIFO_DATA       0x07
#define REG_FIFO_CONFIG     0x08
#define REG_MODE_CONFIG     0x09
#define REG_SPO2_CONFIG     0x0A
#define REG_LED1_PA         0x0C
#define REG_LED2_PA         0x0D
#define REG_PILOT_PA        0x10
#define REG_MULTI_LED_CTRL1 0x11
#define REG_MULTI_LED_CTRL2 0x12
#define REG_TEMP_INTR       0x1F
#define REG_TEMP_FRAC       0x20
#define REG_TEMP_CONFIG     0x21
#define REG_PROX_INT_THRESH 0x30
#define REG_REV_ID          0xFE
#define REG_PART_ID         0xFF

uint8_t s_Hr = 0, s_Spo2 = 0;
static int g_state = 0;
uint32_t arrIrBuf[500] = {0};  // IR LED sensor data
uint32_t arrRedBuf[500] = {0}; // Red LED sensor data

// PPG 滤波器实例
static PpgFilter_t g_ir_filter = {0};
static PpgFilter_t g_red_filter = {0};

/*
 * ============================================================================
 * PPG 信号滤波器实现：中值滤波 + EMA 平滑
 * ============================================================================
 */

// 初始化 PPG 滤波器
static void PpgFilterInit(PpgFilter_t *f)
{
    memset(f, 0, sizeof(PpgFilter_t));
    f->initialized = false;
}

// 初始化输出级滤波器
static void OutputFilterInit(OutputFilter_t *f)
{
    memset(f, 0, sizeof(OutputFilter_t));
    f->has_valid_output = false;
}

// 对5个元素进行排序取中值（插入排序，5个元素足够快）
static uint32_t Median5(uint32_t a, uint32_t b, uint32_t c, uint32_t d, uint32_t e)
{
    uint32_t arr[5] = {a, b, c, d, e};
    for (int i = 1; i < 5; i++) {
        uint32_t key = arr[i];
        int j = i - 1;
        while (j >= 0 && arr[j] > key) {
            arr[j + 1] = arr[j];
            j--;
        }
        arr[j + 1] = key;
    }
    return arr[2];  // 返回中间值
}

// PPG 滤波处理：中值滤波去尖峰 + EMA 平滑
static uint32_t PpgFilterProcess(PpgFilter_t *f, uint32_t input)
{
    // 写入环形缓冲
    f->buf[f->idx] = input;
    f->idx = (f->idx + 1) % MEDIAN_WINDOW_SIZE;
    
    // 第一阶段：中值滤波（去除手指移动造成的尖峰）
    uint32_t median_val;
    if (!f->initialized) {
        // 窗口未填满前直接使用原始值
        f->ema_output = (float)input;
        if (f->idx == 0) {
            f->initialized = true;  // 缓冲区填满一圈
        }
        return input;
    }
    
    median_val = Median5(f->buf[0], f->buf[1], f->buf[2], f->buf[3], f->buf[4]);
    
    // 第二阶段：EMA 平滑（抑制高频噪声）
    // output = alpha * median + (1 - alpha) * prev_output
    f->ema_output = EMA_ALPHA * (float)median_val + (1.0f - EMA_ALPHA) * f->ema_output;
    
    return (uint32_t)(f->ema_output + 0.5f);
}

/*
 * ============================================================================
 * 信号质量检测（SQI）：检测PPG信号是否受运动伪影污染
 * 通过计算最近100个IR样本的方差来判断信号稳定性
 * ============================================================================
 */
static bool CheckSignalQuality(uint32_t *irBuf, int startIdx, int len)
{
    if (len < 20) return false;
    
    // 计算最近采样段的均值
    uint64_t sum = 0;
    int checkLen = (len > 100) ? 100 : len;
    int begin = startIdx + len - checkLen;
    if (begin < 0) begin = 0;
    
    for (int i = begin; i < startIdx + len; i++) {
        sum += irBuf[i];
    }
    uint32_t mean = (uint32_t)(sum / checkLen);
    
    // 计算方差
    uint64_t variance = 0;
    for (int i = begin; i < startIdx + len; i++) {
        int64_t diff = (int64_t)irBuf[i] - (int64_t)mean;
        variance += (uint64_t)(diff * diff);
    }
    variance /= checkLen;
    
    return (variance < SQI_VARIANCE_THRESHOLD);
}

/*
 * ============================================================================
 * 输出级滤波：限幅 + 历史中值 + HR-SpO2 交叉验证
 * ============================================================================
 */
static void OutputFilterProcess(OutputFilter_t *f, int32_t rawHr, int8_t hrValid,
                                int32_t rawSpO2, int8_t spo2Valid, bool sqiGood,
                                uint8_t *outHr, uint8_t *outSpO2)
{
    bool hrOk = (hrValid == 1 && rawHr >= HR_VALID_MIN && rawHr <= HR_VALID_MAX);
    bool spo2Ok = (spo2Valid == 1 && rawSpO2 >= SPO2_VALID_MIN && rawSpO2 <= SPO2_VALID_MAX);
    
    /* HR-SpO2 交叉验证：如果心率无效，血氧大概率也不可靠 */
    if (!hrOk) {
        spo2Ok = false;
    }
    
    /* 信号质量差时，不信任新的读数 */
    if (!sqiGood) {
        hrOk = false;
        spo2Ok = false;
    }
    
    if (hrOk && spo2Ok) {
        uint8_t hr = (uint8_t)rawHr;
        uint8_t spo2 = (uint8_t)rawSpO2;
        
        /* 限幅：限制单次变化幅度 */
        if (f->has_valid_output) {
            int hrDiff = (int)hr - (int)f->last_valid_hr;
            if (hrDiff > HR_MAX_CHANGE_PER_CYCLE) {
                hr = f->last_valid_hr + HR_MAX_CHANGE_PER_CYCLE;
            } else if (hrDiff < -HR_MAX_CHANGE_PER_CYCLE) {
                hr = f->last_valid_hr - HR_MAX_CHANGE_PER_CYCLE;
            }
            
            int spo2Diff = (int)spo2 - (int)f->last_valid_spo2;
            if (spo2Diff > SPO2_MAX_CHANGE_PER_CYCLE) {
                spo2 = f->last_valid_spo2 + SPO2_MAX_CHANGE_PER_CYCLE;
            } else if (spo2Diff < -SPO2_MAX_CHANGE_PER_CYCLE) {
                spo2 = f->last_valid_spo2 - SPO2_MAX_CHANGE_PER_CYCLE;
            }
        }
        
        /* 存入历史缓冲 */
        f->hr_history[f->history_idx] = hr;
        f->spo2_history[f->history_idx] = spo2;
        f->history_idx = (f->history_idx + 1) % OUTPUT_HISTORY_SIZE;
        if (f->history_count < OUTPUT_HISTORY_SIZE) {
            f->history_count++;
        }
        
        /* 对历史值取中值输出 */
        if (f->history_count >= 3) {
            uint8_t tmpHr[OUTPUT_HISTORY_SIZE];
            uint8_t tmpSpo2[OUTPUT_HISTORY_SIZE];
            memcpy(tmpHr, f->hr_history, f->history_count);
            memcpy(tmpSpo2, f->spo2_history, f->history_count);
            // 简单冒泡排序取中值
            for (int i = 0; i < f->history_count - 1; i++) {
                for (int j = 0; j < f->history_count - 1 - i; j++) {
                    if (tmpHr[j] > tmpHr[j+1]) {
                        uint8_t t = tmpHr[j]; tmpHr[j] = tmpHr[j+1]; tmpHr[j+1] = t;
                    }
                    if (tmpSpo2[j] > tmpSpo2[j+1]) {
                        uint8_t t = tmpSpo2[j]; tmpSpo2[j] = tmpSpo2[j+1]; tmpSpo2[j+1] = t;
                    }
                }
            }
            hr = tmpHr[f->history_count / 2];
            spo2 = tmpSpo2[f->history_count / 2];
        }
        
        f->last_valid_hr = hr;
        f->last_valid_spo2 = spo2;
        f->has_valid_output = true;
        f->invalid_count = 0;
        
        *outHr = hr;
        *outSpO2 = spo2;
    } else {
        /* 数据无效：沿用上次有效值，直到连续无效次数超限 */
        f->invalid_count++;
        if (f->has_valid_output && f->invalid_count < INVALID_COUNT_THRESHOLD) {
            *outHr = f->last_valid_hr;
            *outSpO2 = f->last_valid_spo2;
        } else {
            /* 连续无效太多次，重置输出 */
            *outHr = 0;
            *outSpO2 = 0;
            f->has_valid_output = false;
            f->history_count = 0;
            f->history_idx = 0;
        }
    }
}

static I2cReset(void)
{
    IoTI2cDeinit(MAX30102_I2C_IDX);
    IoTI2cInit(MAX30102_I2C_IDX, MAX30102_I2C_BAUDRATE);
}
/*
* 函数名称 : Max30102WriteReg
* 功能描述 : max30102写寄存器
* 参    数 : addr   - 寄存器地址
             data   - 数据内容
* 返回值   : 成功返回0,失败返回-1
* 示    例 : Max30102WriteReg(addr,data);
*/
int Max30102WriteReg(uint8_t addr, uint8_t data)
{
    uint8_t buffer[2] = {addr, data};
    IoTI2cSetBaudrate(MAX30102_I2C_IDX, MAX30102_I2C_BAUDRATE);
    uint32_t ret = IoTI2cWrite(MAX30102_I2C_IDX, MAX30102_ADDR, buffer, ARRAY_SIZE(buffer));
    if (ret != IOT_SUCCESS)
    {
        g_state = 0;
        I2cReset();
        printf("max30102 write failed.\n");
        return -1;
    } //
    g_state = 1;
    return 0;
}

/*
* 函数名称 : Max30102ReadReg
* 功能描述 : max30102读寄存器
* 参    数 : addr   - 寄存器地址
             pData       - 数据指针
             len        - 数据长度
* 返回值   : 读取结果
* 示    例 : Max30102ReadReg(uch_addr,&data,len);
*/
int Max30102ReadReg(uint8_t addr, uint8_t *pData, uint16_t len)
{
    if (NULL == pData)
    {
        return -1;
    }
    IoTI2cSetBaudrate(MAX30102_I2C_IDX, MAX30102_I2C_BAUDRATE);
    uint32_t ret = IoTI2cWrite(MAX30102_I2C_IDX, MAX30102_ADDR, &addr, 1);
    if (ret != IOT_SUCCESS)
    {
        g_state = 0;
        I2cReset();
        return -1;
    }

    ret = IoTI2cRead(MAX30102_I2C_IDX, MAX30102_ADDR, pData, len);
    if (ret != IOT_SUCCESS)
    {
        g_state = 0;
        I2cReset();
        printf("max30102 read failed.\n");
        return -1;
    }
    g_state = 1;
    return 0;
}

/*
* 函数名称 : GetMax30102State
* 功能描述 :获取血氧心率传感器上电状态
* 参    数 : 无
* 返回值   : 无
* 示    例 : state = GetMax30102State();
*/
int GetMax30102State()
{
    return g_state;
}

GpioIsrCallbackFunc KeyIntFunc(void)
{
    osSemaphoreRelease(g_hrso2_sensor_sem);
}

void Max30102Init(void)
{
    IoTUartDeinit(1);
    IoTI2cInit(MAX30102_I2C_IDX, MAX30102_I2C_BAUDRATE);
    // 中断引脚初始化
    IoTGpioInit(MAX30102_INT_GPIO);
    IoTGpioSetDir(MAX30102_INT_GPIO, IOT_GPIO_DIR_IN);
    g_hrso2_sensor_sem = osSemaphoreNew(1, 0, NULL);
    IoTGpioRegisterIsrFunc(MAX30102_INT_GPIO, IOT_INT_TYPE_EDGE, IOT_GPIO_EDGE_FALL_LEVEL_LOW, KeyIntFunc, NULL);
    Max30102Reset();

    Max30102WriteReg(REG_INTR_ENABLE_1, 0x80); // INTR setting
    Max30102WriteReg(REG_INTR_ENABLE_2, 0x00);
    Max30102WriteReg(REG_FIFO_WR_PTR, 0x00); // FIFO_WR_PTR[4:0]
    Max30102WriteReg(REG_OVF_COUNTER, 0x00); // OVF_COUNTER[4:0]
    Max30102WriteReg(REG_FIFO_RD_PTR, 0x00); // FIFO_RD_PTR[4:0]
    Max30102WriteReg(REG_FIFO_CONFIG, 0x21); // sample avg = 8, fifo rollover=false, fifo almost full = 32
    Max30102WriteReg(REG_MODE_CONFIG, 0x03); // 0x02 for Red only, 0x03 for SpO2 mode 0x07 multimode LED
    Max30102WriteReg(REG_SPO2_CONFIG, 0x2B); // SPO2_ADC range = 4096nA, SPO2 sample rate (100 Hz), LED pulseWidth (400uS)
    Max30102WriteReg(REG_LED1_PA, 0x2f);     // Choose value for ~ 7mA for LED1
    Max30102WriteReg(REG_LED2_PA, 0x2f);     // Choose value for ~ 7mA for LED2
    Max30102WriteReg(REG_PILOT_PA, 0x7f);    // Choose value for ~ 25mA for Pilot LED

    // 初始化 PPG 信号滤波器（中值滤波 + EMA 平滑）
    PpgFilterInit(&g_ir_filter);
    PpgFilterInit(&g_red_filter);
    OutputFilterInit(&g_output_filter);
    printf("[MAX30102] PPG filter initialized (median=%d, ema_alpha=%.2f)\n",
           MEDIAN_WINDOW_SIZE, EMA_ALPHA);

    uint8_t temp = 0;
    Max30102ReadReg(REG_INTR_STATUS_1, &temp, sizeof(temp));
    Max30102ReadReg(REG_INTR_STATUS_2, &temp, sizeof(temp));
}

void Max30102Reset(void)
{
    Max30102WriteReg(REG_MODE_CONFIG, 0x40);
    Max30102WriteReg(REG_MODE_CONFIG, 0x40);
}

/*
* 函数名称 : Max30102FifoReadBytes
* 功能描述 : max30102 fifo数据读取
* 参    数
             Data - 数据指针
* 返回值   : 无
* 示    例 : Max30102FifoReadBytes();
*/
int Max30102FifoReadBytes(uint8_t *pData)
{
    uint8_t temp;
    // read and clear status register
    int ret = Max30102ReadReg(REG_INTR_STATUS_1, &temp, sizeof(temp));
    ret += Max30102ReadReg(REG_INTR_STATUS_2, &temp, sizeof(temp));

    ret += Max30102ReadReg(REG_FIFO_DATA, pData, 96);
    if (ret)
    {
        printf("max30102 read fifo failed.\n");
    } //

    return ret;
}

/*
 * 函数名称 : Max30102GetHeartRate
 * 功能描述 : 获取心率
 * 参    数 : 无
 * 返回值   : 心率
 * 示    例 : HeartRate = Max30102GetHeartRate();
 */
uint8_t Max30102GetHeartRate(void)
{
    return s_Hr; // 传递心率值
}
/*
 * 函数名称 : Max30102GetSpO2
 * 功能描述 : 获取检测温度
 * 参    数 : 无
 * 返回值   : 血氧饱和度
 * 示    例 : s_Spo2 = Max30102GetSpO2();
 */
uint8_t Max30102GetSpO2(void)
{
    return s_Spo2; // 传递血氧饱和度值
}
/*
 * 函数名称 : Max30102GetFingerStatus
 * 功能描述 : MAX30102任务，获取手指是否放在传感器的状态
 * 参    数 : 无
 * 返回值   : 手指状态
 * 示    例 : finger_status = Max30102GetFingerStatus();
 */
bool Max30102GetFingerStatus(void)
{
    return finger;
}

/*
 * 函数名称 : MAX30102Task
 * 功能描述 : MAX30102任务，计算出心率血氧值
 * 参    数 : 无
 * 返回值   : 无
 * 示    例 : MAX30102Task();
 */
void Max30102Task(void *arg)
{
    Max30102Init();
    int j = 0;
    uint8_t id = 0;
    Max30102ReadReg(REG_PART_ID, &id, 1);
    printf("[%s-%d]id = %d \n", __FUNCTION__, __LINE__, id);

    uint8_t temp[96] = {0};
    float fTemp = 0.0;
    uint32_t unMin = 0, unMax = 0, unPrevData = 0;
    int32_t brightness = 0;

    static uint8_t disHrBank = 0;
    static uint8_t disSpO2Bank = 0;
    static uint8_t cnt = 0;

    int i = 0;
    uint8_t value = 0;
    int irBufLen = 500; // buffer length of 100 stores 5 seconds of samples running at 100sps

    int32_t nSpO2;      // SPO2 value
    int8_t chSpO2Valid; // indicator to show if the SP02 calculation is valid
    int32_t nHeartRate; // heart rate value
    int8_t chHrValid;   // indicator to show if the heart rate calculation is valid
    int ret = 0;
    // read the first 500 samples, and determine the signal range
    for (i = 0; i < irBufLen; i++)
    {
        if (j == 0)
        {
            osSemaphoreAcquire(g_hrso2_sensor_sem, 0xffffff);
            Max30102FifoReadBytes(temp);
        }
        arrRedBuf[i] = ((long)temp[j] & 0x03) << 16 | (long)temp[j + 1] << 8 | (long)temp[j + 2];    // Combine values to get the actual number
        arrIrBuf[i] = ((long)temp[j + 3] & 0x03) << 16 | (long)temp[j + 4] << 8 | (long)temp[j + 5]; // Combine values to get the actual number

        // 对采样数据应用 PPG 滤波（中值去尖峰 + EMA 平滑）
        arrRedBuf[i] = PpgFilterProcess(&g_red_filter, arrRedBuf[i]);
        arrIrBuf[i] = PpgFilterProcess(&g_ir_filter, arrIrBuf[i]);

        j = j + 6;
        if (j == 96)
            j = 0;
    }
    printf("[MAX30102] Initial 500 samples filtered (median+EMA)\n");

    // unPrevData = arrRedBuf[i];
    // //calculate heart rate and SpO2 after first 500 samples (first 5 seconds of samples)
    maxim_heart_rate_and_oxygen_saturation(arrIrBuf, irBufLen, arrRedBuf, &nSpO2, &chSpO2Valid, &nHeartRate, &chHrValid);

    while (1)
    {
        i = 0;
        unMin = 0x3FFFF;
        unMax = 0;

        // memcpy(arrRedBuf,arrRedBuf[100],400);
        // dumping the first 100 sets of samples in the memory and shift the last 400 sets of samples to the top
        for (i = 100; i < 500; i++)
        {
            arrRedBuf[i - 100] = arrRedBuf[i];
            arrIrBuf[i - 100] = arrIrBuf[i];
        } //

        // take 100 sets of samples before calculating the heart rate.
        for (i = 400; i < 500; i++)
        {
            unPrevData = arrRedBuf[i - 1];
            if (j == 0)
            {
                osSemaphoreAcquire(g_hrso2_sensor_sem, 0xffffff);
                Max30102FifoReadBytes(temp);
                j = 0;
            }
            arrRedBuf[i] = (long)((long)((long)temp[j] & 0x03) << 16) | (long)temp[j + 1] << 8 | (long)temp[j + 2];    // Combine values to get the actual number
            arrIrBuf[i] = (long)((long)((long)temp[j + 3] & 0x03) << 16) | (long)temp[j + 4] << 8 | (long)temp[j + 5]; // Combine values to get the actual number
            
            // 对新读取的样本应用 PPG 滤波（中值去尖峰 + EMA 平滑）
            arrRedBuf[i] = PpgFilterProcess(&g_red_filter, arrRedBuf[i]);
            arrIrBuf[i] = PpgFilterProcess(&g_ir_filter, arrIrBuf[i]);
            
            j = j + 6;
            // printf("count=%d\n",j)
            if (j == 96)
            {
                j = 0;
            }
            // printf("<any>:%d,%d\n",arrIrBuf[i],arrRedBuf[i]);

            if (arrIrBuf[i] < 10000)
            {
                break;
            }
        }     //

        if (i >= 500)
        {
            finger = true;
            
            /* 信号质量检测：检查最近采样段的方差 */
            bool sqiGood = CheckSignalQuality(arrIrBuf, 400, 100);
            
            maxim_heart_rate_and_oxygen_saturation(arrIrBuf, irBufLen, arrRedBuf, &nSpO2, &chSpO2Valid, &nHeartRate, &chHrValid);

            /* 通过输出级滤波器处理：限幅 + 异常拒绝 + 历史中值 + HR-SpO2交叉验证 */
            uint8_t filteredHr = 0, filteredSpO2 = 0;
            OutputFilterProcess(&g_output_filter, nHeartRate, chHrValid,
                               nSpO2, chSpO2Valid, sqiGood,
                               &filteredHr, &filteredSpO2);
            
            s_Hr = filteredHr;
            s_Spo2 = filteredSpO2;

            printf("HR=%d SpO2=%d (raw:%d/%d valid:%d/%d sqi:%d)\n",
                   s_Hr, s_Spo2, (int)nHeartRate, (int)nSpO2,
                   chHrValid, chSpO2Valid, sqiGood);
        }
        else
        {
            s_Hr = 0;
            s_Spo2 = 0;
            if (finger == true)
            {
                finger = false;
                PpgFilterInit(&g_ir_filter);
                PpgFilterInit(&g_red_filter);
                OutputFilterInit(&g_output_filter);
                printf("HR = %d, SpO2 = %d, no finger!!!\n", s_Hr, s_Spo2);
            }
            osDelay(5);
        } //
        // usleep(50);
    } //
}

/*
 * 函数名称 : StartMax30102Task
 * 功能描述 : 启动Max30102任务
 * 参    数 : 空
 * 返回值   : 空
 * 示    例 : StartMax30102Task();
 */
int StartMax30102Task(void)
{
    if (NULL != g_tid)
    {
        return 0;
    }

    osThreadAttr_t attr;
    attr.name = "Max30102Task";
    attr.attr_bits = 0U;
    attr.cb_mem = NULL;
    attr.cb_size = 0U;
    attr.stack_mem = NULL;
    attr.stack_size = MAX30102_TASK_STACK_SIZE;
    attr.priority = MAX30102_TASK_PRIO;

    int ret = 0;
    g_tid = osThreadNew((osThreadFunc_t)Max30102Task, NULL, &attr);
    if (NULL == g_tid)
    {
        ret = -1;
    }

    return ret;
}

void StopMax30102Task(void)
{
    if (NULL == g_tid)
    {
        return;
    }

    osThreadState_t tidStatus = osThreadInactive;

    tidStatus = osThreadGetState(g_tid);
    if (tidStatus <= osThreadBlocked && tidStatus > osThreadInactive)
    {
        osThreadTerminate(g_tid);
        // printf("kill g_tid :%d, g_tid_status is %d\r\n", g_tid, g_tid_status);
        osDelay(200);
        // printf("g_tid = NULL :%d, g_tid_status is %d\r\n", g_tid, g_tid_status);
        g_tid = NULL;
    }
}

// APP_FEATURE_INIT(StartMax30102Task);
