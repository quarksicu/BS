/*
 * Copyright (c) 2021 Shenzhen Kaihong Digital Industry Development Co.,Ltd.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <stdio.h>
#include <string.h>
#include "ohos_init.h"
#include "ohos_types.h"
#include "cmsis_os2.h"
#include "iot_uart.h"
#include "iot_gpio.h"

/* license_register stub函数 - 当data_receive模块未编译时提供空实现 */
typedef unsigned int (*license_msg_handler_t)(void* msg, unsigned int dataSize);
__attribute__((weak)) void license_register(license_msg_handler_t msg_handler)
{
    (void)msg_handler;  /* 避免未使用参数警告 */
}

/* 串口配置参数 */
#define UART_PORT           0       /* 使用UART0作为USB串口 */
#define UART_BAUD_RATE      115200  /* 波特率115200 */
#define SENSOR_READ_INTERVAL 10     /* 传感器读取间隔(10ms为单位) 即100ms，每秒10次 */
#define OLED_UPDATE_COUNT    5      /* 每5次采样更新一次OLED显示(500ms) */
#define UART_TX_BUFFER_SIZE 256     /* 串口发送缓冲区大小 */

/* 异常阈值定义 */
#define HR_MIN_VALID        10      /* 心率有效最小值(排除无效数据) */
#define HR_LOW_THRESHOLD    50      /* 心率过低阈值 */
#define HR_HIGH_THRESHOLD   120     /* 心率过高阈值 */
#define SPO2_MIN_VALID      10      /* 血氧有效最小值(排除无效数据) */
#define SPO2_LOW_THRESHOLD  90      /* 血氧过低阈值 */
#define TEMP_MIN_VALID      20.0f   /* 体温有效最小值(排除无效数据) */
#define TEMP_HIGH_THRESHOLD 37.5f   /* 体温过高阈值(远红外测温仅提示高温) */
#define ALERT_BEEP_DURATION 50      /* 蜂鸣器响持续时间(10ms为单位) */
#define ALERT_BEEP_PAUSE    30      /* 蜂鸣器停顿时间(10ms为单位) */
#define ALERT_BEEP_REPEAT   3       /* 蜂鸣器重复次数 */

/* 外部函数声明 - 红外测温传感器 */
extern void Mlx90614Init(void);
extern int GetTemp(float *temp);

/* 外部函数声明 - 心率血氧传感器 */
extern void Max30102Init(void);
extern void StartMax30102Task(void);
extern uint8_t Max30102GetHeartRate(void);
extern uint8_t Max30102GetSpO2(void);

/* 外部函数声明 - OLED显示 */
extern void OledInit(void);
extern void OledShowString(uint8_t x, uint8_t y, const uint8_t *p, uint8_t size);
extern void OledRefreshGram(void);
extern void OledClear(void);

/* 外部函数声明 - 蜂鸣器 */
extern void BeepInit(void);
extern void BeepStart(void);
extern void BeepStop(void);

/* 初始化UART串口 */
static int InitUart(void)
{
    IotUartAttribute param = {
        .baudRate = UART_BAUD_RATE,
        .dataBits = IOT_UART_DATA_BIT_8,
        .parity = IOT_UART_PARITY_NONE,
        .stopBits = IOT_UART_STOP_BIT_1,
        .rxBlock = IOT_UART_BLOCK_STATE_NONE_BLOCK,
    };
    
    IoTUartDeinit(UART_PORT);
    if (IoTUartInit(UART_PORT, &param) != 0) {
        printf("UART init failed!\r\n");
        return -1;
    }
    printf("UART init success!\r\n");
    return 0;
}

/* 通过串口发送数据 */
static void SendDataToUart(const char *data, uint32_t len)
{
    IoTUartWrite(UART_PORT, (const unsigned char *)data, len);
}

/* 检测健康数据是否异常（返回1表示异常，0表示正常或无效） */
typedef struct {
    uint8_t hrAlert;     /* 心率异常: 0正常, 1过低, 2过高 */
    uint8_t spo2Alert;   /* 血氧异常: 0正常, 1过低 */
    uint8_t tempAlert;   /* 体温异常: 0正常, 1过低, 2过高 */
} AlertStatus;

static AlertStatus CheckHealthAlert(uint8_t heartRate, uint8_t spO2, float temperature)
{
    AlertStatus alert = {0, 0, 0};
    
    /* 心率检测: 只在有效范围内判断异常 */
    if (heartRate >= HR_MIN_VALID) {
        if (heartRate < HR_LOW_THRESHOLD) {
            alert.hrAlert = 1;
        } else if (heartRate > HR_HIGH_THRESHOLD) {
            alert.hrAlert = 2;
        }
    }
    
    /* 血氧检测: 只在有效范围内判断异常 */
    if (spO2 >= SPO2_MIN_VALID) {
        if (spO2 < SPO2_LOW_THRESHOLD) {
            alert.spo2Alert = 1;
        }
    }
    
    /* 体温检测: 远红外测温可能不准确，仅提示高温 */
    if (temperature >= TEMP_MIN_VALID && temperature > TEMP_HIGH_THRESHOLD) {
        alert.tempAlert = 2;
    }
    
    return alert;
}

/* 蜂鸣器报警（短促蜂鸣多次） */
static void TriggerBeepAlert(void)
{
    for (int i = 0; i < ALERT_BEEP_REPEAT; i++) {
        BeepStart();
        osDelay(ALERT_BEEP_DURATION);
        BeepStop();
        if (i < ALERT_BEEP_REPEAT - 1) {
            osDelay(ALERT_BEEP_PAUSE);
        }
    }
}

/* 更新OLED显示内容（含异常警报） */
static void UpdateOledDisplay(uint8_t heartRate, uint8_t spO2, float temperature, AlertStatus alert)
{
    char str[32] = {0};
    uint8_t hasAlert = (alert.hrAlert || alert.spo2Alert || alert.tempAlert);
    
    OledClear();
    
    /* 显示心率 */
    if (alert.hrAlert == 1) {
        snprintf(str, sizeof(str), "HR:%d LOW!", heartRate);
    } else if (alert.hrAlert == 2) {
        snprintf(str, sizeof(str), "HR:%d HIGH!", heartRate);
    } else {
        snprintf(str, sizeof(str), "HR: %d bpm", heartRate);
    }
    OledShowString(0, 0, (uint8_t *)str, 16);
    
    /* 显示血氧 */
    if (alert.spo2Alert == 1) {
        snprintf(str, sizeof(str), "SpO2:%d%% LOW!", spO2);
    } else {
        snprintf(str, sizeof(str), "SpO2: %d %%", spO2);
    }
    OledShowString(0, 16, (uint8_t *)str, 16);
    
    /* 显示体温 */
    if (alert.tempAlert == 2) {
        snprintf(str, sizeof(str), "T:%.1fC HIGH!", temperature);
    } else {
        snprintf(str, sizeof(str), "Temp: %.1f C", temperature);
    }
    OledShowString(0, 32, (uint8_t *)str, 16);
    
    /* 第四行显示警报总提示 */
    if (hasAlert) {
        OledShowString(0, 48, (uint8_t *)"!! ALERT !!", 16);
    }
    
    OledRefreshGram();
}

/* 健康监测任务 - 采集并发送心率、血氧、体温数据 */
static void HealthMonitorTask(void)
{
    char txBuffer[UART_TX_BUFFER_SIZE] = {0};
    uint8_t heartRate = 0;
    uint8_t spO2 = 0;
    float temperature = 0.0f;
    
    /* 初始化串口 */
    if (InitUart() != 0) {
        printf("Failed to init UART!\r\n");
        return;
    }
    
    /* 先初始化OLED（会初始化I2C1） */
    OledInit();
    printf("OLED initialized!\r\n");
    
    /* 然后初始化心率血氧传感器 */
    Max30102Init();
    StartMax30102Task();
    printf("MAX30102 initialized!\r\n");
    
    /* 初始化蜂鸣器 */
    BeepInit();
    printf("Buzzer initialized!\r\n");
    
    /* 最后初始化红外测温传感器 */
    Mlx90614Init();
    printf("MLX90614 initialized!\r\n");
    
    /* 发送启动信息 */
    const char *startMsg = "\r\n=== Health Monitor Started (10Hz) ===\r\n";
    SendDataToUart(startMsg, strlen(startMsg));
    
    uint32_t sampleCount = 0;  /* 采样计数器 */
    uint32_t oledCounter = 0;  /* OLED更新计数器 */
    
    while (1) {
        /* 读取心率和血氧值 */
        heartRate = Max30102GetHeartRate();
        /* 心率校准: 算法输出整体偏高约10BPM，此处补偿 */
        if (heartRate > 10) {
            heartRate -= 10;
        }
        spO2 = Max30102GetSpO2();
        
        /* 读取体温值 */
        if (GetTemp(&temperature) != 0) {
            temperature = 0.0f; /* 读取失败时设为0 */
        }
        
        /* 格式化数据并通过串口发送 */
        /* 格式: N:序号,HR:心率,SpO2:血氧,TEMP:体温 */
        memset(txBuffer, 0, UART_TX_BUFFER_SIZE);
        snprintf(txBuffer, UART_TX_BUFFER_SIZE,
                 "%u,%d,%d,%.1f\r\n",
                 sampleCount, heartRate, spO2, temperature);
        
        SendDataToUart(txBuffer, strlen(txBuffer));
        sampleCount++;
        
        /* 每OLED_UPDATE_COUNT次采样更新一次OLED显示，并检测异常 */
        oledCounter++;
        if (oledCounter >= OLED_UPDATE_COUNT) {
            AlertStatus alert = CheckHealthAlert(heartRate, spO2, temperature);
            UpdateOledDisplay(heartRate, spO2, temperature, alert);
            
            /* 有异常时触发蜂鸣器报警 */
            if (alert.hrAlert || alert.spo2Alert || alert.tempAlert) {
                TriggerBeepAlert();
            }
            oledCounter = 0;
        }
        
        /* 延时一段时间再进行下一次采集 */
        osDelay(SENSOR_READ_INTERVAL);
    }
}

/* 应用入口函数 */
static void HealthMonitor(void)
{
    printf("\n=== HealthMonitor Application ===\n");
    
    osThreadAttr_t attr;
    attr.name = "HealthMonitorTask";
    attr.attr_bits = 0U;
    attr.cb_mem = NULL;
    attr.cb_size = 0U;
    attr.stack_mem = NULL;
    attr.stack_size = 0x1000; /* 4KB栈空间 */
    attr.priority = 25;

    if (osThreadNew((osThreadFunc_t)HealthMonitorTask, NULL, &attr) == NULL) {
        printf("Failed to create HealthMonitorTask!\r\n");
    } else {
        printf("HealthMonitorTask created successfully!\r\n");
    }
}

APP_FEATURE_INIT(HealthMonitor);
