#ifndef __MAX30102_H_
#define __MAX30102_H_

#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdbool.h>

void Max30102Init(void);
void Max30102Reset(void);
uint8_t Max30102GetHeartRate();
uint8_t Max30102GetSpO2();
bool Max30102GetFingerStatus();
int GetMax30102State();
int StartMax30102Task(void);
void StopMax30102Task(void);

int Max30102WriteReg(uint8_t addr, uint8_t data);
int Max30102ReadReg(uint8_t addr, uint8_t *pData, uint16_t len);


void Max30102Task(void *arg);
#endif // !__MAX30102_H
