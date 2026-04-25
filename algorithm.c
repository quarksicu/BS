/*******************************************************************************
* Copyright (C) 2016 Maxim Integrated Products, Inc., All Rights Reserved.
*
* Permission is hereby granted, free of charge, to any person obtaining a
* copy of this software and associated documentation files (the "Software"),
* to deal in the Software without restriction, including without limitation
* the rights to use, copy, modify, merge, publish, distribute, sublicense,
* and/or sell copies of the Software, and to permit persons to whom the
* Software is furnished to do so, subject to the following conditions:
*
* The above copyright notice and this permission notice shall be included
* in all copies or substantial portions of the Software.
*
* THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
* OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
* MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
* IN NO EVENT SHALL MAXIM INTEGRATED BE LIABLE FOR ANY CLAIM, DAMAGES
* OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
* ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
* OTHER DEALINGS IN THE SOFTWARE.
*
* Except as contained in this notice, the name of Maxim Integrated
* Products, Inc. shall not be used except as stated in the Maxim Integrated
* Products, Inc. Branding Policy.
*
* The mere transfer of this software does not imply any licenses
* of trade secrets, proprietary technology, copyrights, patents,
* trademarks, maskwork rights, or any other form of intellectual
* property whatsoever. Maxim Integrated Products, Inc. retains all
* ownership rights.
*******************************************************************************
*/
#include "algorithm.h"

const uint16_t auw_hamm[31]={ 41,    276,    512,    276,     41 }; //Hamm=  long16(512* hamming(5)');
//uch_spo2_table is computed as  -45.060*ratioAverage* ratioAverage + 30.354 *ratioAverage + 94.845 ;
const uint8_t uch_spo2_table[184]={ 95, 95, 95, 96, 96, 96, 97, 97, 97, 97, 97, 98, 98, 98, 98, 98, 99, 99, 99, 99, 
                            99, 99, 99, 99, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 
                            100, 100, 100, 100, 99, 99, 99, 99, 99, 99, 99, 99, 98, 98, 98, 98, 98, 98, 97, 97, 
                            97, 97, 96, 96, 96, 96, 95, 95, 95, 94, 94, 94, 93, 93, 93, 92, 92, 92, 91, 91, 
                            90, 90, 89, 89, 89, 88, 88, 87, 87, 86, 86, 85, 85, 84, 84, 83, 82, 82, 81, 81, 
                            80, 80, 79, 78, 78, 77, 76, 76, 75, 74, 74, 73, 72, 72, 71, 70, 69, 69, 68, 67, 
                            66, 66, 65, 64, 63, 62, 62, 61, 60, 59, 58, 57, 56, 56, 55, 54, 53, 52, 51, 50, 
                            49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 31, 30, 29, 
                            28, 27, 26, 25, 23, 22, 21, 20, 19, 17, 16, 15, 14, 12, 11, 10, 9, 7, 6, 5, 
                            3, 2, 1 } ;
static  int32_t an_dx[ BUFFER_SIZE-MA4_SIZE]; // delta
static  int32_t an_x[ BUFFER_SIZE]; //ir
static  int32_t an_y[ BUFFER_SIZE]; //red
static  int32_t an_dwt_temp[ BUFFER_SIZE]; // DWT pre-filter temp buffer

/*
 * ============================================================================
 * DWT-inspired 5-tap Binomial FIR Pre-filter
 *
 * Approximates db4 Discrete Wavelet Transform denoising (level 1-2) using
 * a single-pass FIR with coefficients [1, 4, 6, 4, 1] / 16.
 * Frequency response:  |H(f)| = cos^4(pi * f / Fs)
 *
 *   D1  25-50 Hz : -12 dB to -inf dB  -- equivalent to zeroing D1 in DWT
 *   D2  12.5-25Hz: -1.4 to -12 dB     -- equivalent to soft-threshold D2
 *   D3/D4/A4 <12.5Hz: < -1.4 dB      -- PPG harmonics fully preserved
 *
 * Applied as Stage 1 before VS-LMS to provide a cleaner IR signal, so the
 * IR-Red reference contains less ADC/electronic noise and VS-LMS can focus
 * purely on correlated motion artifact cancellation.
 *
 * Uses static buffer (an_dwt_temp) -- no heap, no stack pressure.
 * Edge handling: constant-extension (clamp) at both boundaries.
 * ============================================================================
 */
static void maxim_dwt_filter(int32_t *pn_ir, int32_t n_length)
{
    int32_t k;

    /* k = 0  (clamped: x[-2]=x[-1]=x[0]) */
    an_dwt_temp[0] = (11*pn_ir[0] + (pn_ir[1] << 2) + pn_ir[2] + 8) >> 4;

    /* k = 1  (clamped: x[-1]=x[0]) */
    an_dwt_temp[1] = (5*pn_ir[0] + 6*pn_ir[1] + (pn_ir[2] << 2) + pn_ir[3] + 8) >> 4;

    /* Main body: k = 2 .. n-3 (no boundary effects) */
    for (k = 2; k < n_length - 2; k++) {
        an_dwt_temp[k] = (pn_ir[k-2] + (pn_ir[k-1] << 2) + 6*pn_ir[k]
                          + (pn_ir[k+1] << 2) + pn_ir[k+2] + 8) >> 4;
    }

    /* k = n-2  (clamped: x[n]=x[n-1]) */
    an_dwt_temp[n_length-2] = (pn_ir[n_length-4] + (pn_ir[n_length-3] << 2)
                               + 6*pn_ir[n_length-2] + 5*pn_ir[n_length-1] + 8) >> 4;

    /* k = n-1  (clamped: x[n]=x[n+1]=x[n-1]) */
    an_dwt_temp[n_length-1] = (pn_ir[n_length-3] + (pn_ir[n_length-2] << 2)
                               + 11*pn_ir[n_length-1] + 8) >> 4;

    /* Write filtered result back in-place */
    for (k = 0; k < n_length; k++)
        pn_ir[k] = an_dwt_temp[k];
}

/*
 * ============================================================================
 * VS-LMS (Variable Step-size LMS) Adaptive Filter
 * Based on: "Removal of Motion Artifacts in PPG Signals Based on
 *            CEEMDAN-MPE and VS-LMS Adaptive Filter"
 *
 * Without accelerometer, we use the normalized IR-Red difference as a
 * synthetic motion artifact reference signal. Motion artifacts are highly
 * correlated in both channels, while the PPG AC component differs due to
 * wavelength-dependent absorption.
 *
 * VS-LMS step-size update rule (fixed-point):
 *   mu(n+1) = alpha * mu(n) + gamma * e(n)^2
 *   bounded by [MU_MIN, MU_MAX]
 *
 * All arithmetic is integer/fixed-point (Q15 format) for embedded use.
 * ============================================================================
 */
#define VSLMS_FILTER_ORDER   8       // adaptive filter tap length
#define VSLMS_Q              15      // fixed-point fractional bits
#define VSLMS_ONE            (1 << VSLMS_Q)  // 1.0 in Q15 = 32768
#define VSLMS_MU_INIT        164     // initial step-size ~0.005 in Q15
#define VSLMS_MU_MIN         33      // minimum step-size ~0.001 in Q15
#define VSLMS_MU_MAX         3277    // maximum step-size ~0.1 in Q15
#define VSLMS_ALPHA          31130   // mu decay factor ~0.95 in Q15
#define VSLMS_GAMMA          328     // error adaptation gain ~0.01 in Q15
#define VSLMS_BYPASS_RATIO   4915    // bypass threshold ~0.15 in Q15 (ref_power/ir_power < 15% → skip)
#define VSLMS_W_CLAMP        327680  // weight clamp = 10.0 in Q15

static void maxim_vslms_filter(int32_t *pn_ir, int32_t *pn_red, int32_t n_length)
/**
* \brief        VS-LMS adaptive motion artifact removal
* \par          Details
*               Uses normalized (IR - Red) difference as synthetic reference
*               to adaptively cancel correlated motion artifacts from IR signal.
*               Step-size varies with squared error for fast convergence and low
*               steady-state misadjustment.
*
*               [Optimization 1] Auto-bypass: when reference signal power is low
*               relative to IR (no motion artifact), skip filtering entirely to
*               avoid degrading clean PPG signals.
*
*               [Optimization 2] NLMS normalization: weight update is divided by
*               reference buffer power to prevent divergence with large signals.
*
* \param[in,out]  *pn_ir    - IR signal buffer (filtered in-place)
* \param[in]      *pn_red   - Red signal buffer (used to create reference)
* \param[in]      n_length  - buffer length
*
* \retval       None
*/
{
    int32_t w[VSLMS_FILTER_ORDER] = {0};
    int32_t ref_buf[VSLMS_FILTER_ORDER] = {0};
    int32_t mu = VSLMS_MU_INIT;
    int32_t k, j;
    long long ir_sum = 0, red_sum = 0;
    int32_t ir_mean, red_mean;
    int32_t scale_q15 = VSLMS_ONE;
    int32_t norm_red, ir_ac, ref;
    long long y_acc;
    int32_t y, e, e_clamp, e_sq;
    long long delta;
    long long ir_var = 0, ref_var = 0;
    int32_t ref_power;

    /* ---- Compute DC means ---- */
    for (k = 0; k < n_length; k++) {
        ir_sum += pn_ir[k];
        red_sum += pn_red[k];
    }
    ir_mean = (int32_t)(ir_sum / n_length);
    red_mean = (int32_t)(red_sum / n_length);

    if (red_mean > 0) {
        scale_q15 = (int32_t)(((long long)ir_mean << VSLMS_Q) / red_mean);
    }

    /* ---- [Optimization 1] Signal quality bypass ----
     * Compute variance of IR_AC and reference = scaled_Red_AC - IR_AC.
     * If reference power is less than 15% of IR power, there is no
     * significant motion artifact → skip filtering to preserve signal.
     */
    for (k = 0; k < n_length; k++) {
        ir_ac = pn_ir[k] - ir_mean;
        norm_red = (int32_t)(((long long)(pn_red[k] - red_mean) * scale_q15) >> VSLMS_Q);
        ref = norm_red - ir_ac;
        ir_var += (long long)ir_ac * ir_ac;
        ref_var += (long long)ref * ref;
    }
    /* Compare: ref_var / ir_var < 0.15  →  ref_var * Q15 / ir_var < BYPASS_RATIO */
    if (ir_var > 0) {
        long long ratio_q15 = (ref_var << VSLMS_Q) / ir_var;
        if (ratio_q15 < VSLMS_BYPASS_RATIO) {
            return; /* No motion artifact detected, bypass filtering */
        }
    }

    /* ---- Main VS-LMS filtering loop ---- */
    for (k = 0; k < n_length; k++) {
        norm_red = (int32_t)(((long long)(pn_red[k] - red_mean) * scale_q15) >> VSLMS_Q);
        ir_ac = pn_ir[k] - ir_mean;
        ref = norm_red - ir_ac;

        for (j = VSLMS_FILTER_ORDER - 1; j > 0; j--) {
            ref_buf[j] = ref_buf[j - 1];
        }
        ref_buf[0] = ref;

        /* FIR filter output */
        y_acc = 0;
        for (j = 0; j < VSLMS_FILTER_ORDER; j++) {
            y_acc += (long long)w[j] * ref_buf[j];
        }
        y = (int32_t)(y_acc >> VSLMS_Q);

        /* Error signal = clean PPG estimate */
        e = ir_ac - y;

        /* Clamp error to prevent overflow in squaring */
        e_clamp = e;
        if (e_clamp > 32767) e_clamp = 32767;
        if (e_clamp < -32767) e_clamp = -32767;

        /* VS-LMS variable step-size update */
        e_sq = (int32_t)(((long long)e_clamp * e_clamp) >> VSLMS_Q);
        mu = (int32_t)(((long long)VSLMS_ALPHA * mu + (long long)VSLMS_GAMMA * e_sq) >> VSLMS_Q);
        if (mu < VSLMS_MU_MIN) mu = VSLMS_MU_MIN;
        if (mu > VSLMS_MU_MAX) mu = VSLMS_MU_MAX;

        /* [Optimization 2] NLMS-style weight update: w += (mu/||ref||^2) * e * ref
         * Compute ref buffer power and normalize to prevent divergence */
        ref_power = 0;
        for (j = 0; j < VSLMS_FILTER_ORDER; j++) {
            ref_power += (int32_t)(((long long)ref_buf[j] * ref_buf[j]) >> VSLMS_Q);
        }
        if (ref_power < 1) ref_power = 1; /* avoid divide-by-zero */

        for (j = 0; j < VSLMS_FILTER_ORDER; j++) {
            delta = ((long long)mu * e_clamp) >> VSLMS_Q;
            delta = (delta * ref_buf[j]) / ref_power;
            w[j] += (int32_t)delta;
            /* Clamp weights to prevent divergence */
            if (w[j] > VSLMS_W_CLAMP) w[j] = VSLMS_W_CLAMP;
            if (w[j] < -VSLMS_W_CLAMP) w[j] = -VSLMS_W_CLAMP;
        }

        /* Write filtered output back */
        pn_ir[k] = (uint32_t)(e + ir_mean);
    }
}

void maxim_heart_rate_and_oxygen_saturation(uint32_t *pun_ir_buffer,  int32_t n_ir_buffer_length, uint32_t *pun_red_buffer, int32_t *pn_spo2, int8_t *pch_spo2_valid, 
                              int32_t *pn_heart_rate, int8_t  *pch_hr_valid)
/**
* \brief        Calculate the heart rate and SpO2 level
* \par          Details
*               By detecting  peaks of PPG cycle and corresponding AC/DC of red/infra-red signal, the ratio for the SPO2 is computed.
*               Since this algorithm is aiming for Arm M0/M3. formaula for SPO2 did not achieve the accuracy due to register overflow.
*               Thus, accurate SPO2 is precalculated and save longo uch_spo2_table[] per each ratio.
*
* \param[in]    *pun_ir_buffer           - IR sensor data buffer
* \param[in]    n_ir_buffer_length      - IR sensor data buffer length
* \param[in]    *pun_red_buffer          - Red sensor data buffer
* \param[out]    *pn_spo2                - Calculated SpO2 value
* \param[out]    *pch_spo2_valid         - 1 if the calculated SpO2 value is valid
* \param[out]    *pn_heart_rate          - Calculated heart rate value
* \param[out]    *pch_hr_valid           - 1 if the calculated heart rate value is valid
*
* \retval       None
*/
{
    uint32_t un_ir_mean ,un_only_once ;
    int32_t k ,n_i_ratio_count;
    int32_t i, s, m, n_exact_ir_valley_locs_count ,n_middle_idx;
    int32_t n_th1, n_npks,n_c_min;      
    int32_t an_ir_valley_locs[15] ;
    int32_t an_exact_ir_valley_locs[15] ;
    int32_t an_dx_peak_locs[15] ;
    int32_t n_peak_interval_sum;
    
    int32_t n_y_ac, n_x_ac;
    int32_t n_spo2_calc; 
    int32_t n_y_dc_max, n_x_dc_max; 
    int32_t n_y_dc_max_idx, n_x_dc_max_idx; 
    int32_t an_ratio[5],n_ratio_average; 
    int32_t n_nume,  n_denom ;

    // [VS-LMS fix] Save original IR before filtering for SpO2 calculation
    // VS-LMS modifies IR in-place which distorts AC/DC ratio needed for SpO2
    static uint32_t aun_ir_original[BUFFER_SIZE];
    for (k = 0; k < n_ir_buffer_length; k++)
        aun_ir_original[k] = pun_ir_buffer[k];

    // [DWT+VS-LMS] Two-stage filtering pipeline:
    // Stage 1: DWT pre-filter -- 5-tap binomial FIR removes high-freq ADC noise (>25Hz)
    //          Equivalent to zeroing D1 and attenuating D2 in db4 DWT denoising.
    // Stage 2: VS-LMS adaptive filter -- cancels correlated motion artifacts using
    //          normalized IR-Red difference as synthetic reference.
    // Result:  HR uses doubly-filtered IR; SpO2 uses original IR (saved above).
    maxim_dwt_filter((int32_t *)pun_ir_buffer, n_ir_buffer_length);
    maxim_vslms_filter((int32_t *)pun_ir_buffer, (int32_t *)pun_red_buffer, n_ir_buffer_length);

    // remove DC of ir signal    
    un_ir_mean =0; 
    for (k=0 ; k<n_ir_buffer_length ; k++ ) un_ir_mean += pun_ir_buffer[k] ;
    un_ir_mean =un_ir_mean/n_ir_buffer_length ;
    for (k=0 ; k<n_ir_buffer_length ; k++ )  an_x[k] =  pun_ir_buffer[k] - un_ir_mean ; 
    
    // 4 pt Moving Average
    for(k=0; k< BUFFER_SIZE-MA4_SIZE; k++){
        n_denom= ( an_x[k]+an_x[k+1]+ an_x[k+2]+ an_x[k+3]);
        an_x[k]=  n_denom/(int32_t)4; 
    }

    // get difference of smoothed IR signal
    
    for( k=0; k<BUFFER_SIZE-MA4_SIZE-1;  k++)
        an_dx[k]= (an_x[k+1]- an_x[k]);

    // 2-pt Moving Average to an_dx
    for(k=0; k< BUFFER_SIZE-MA4_SIZE-2; k++){
        an_dx[k] =  ( an_dx[k]+an_dx[k+1])/2 ;
    }
    
    // hamming window
    // flip wave form so that we can detect valley with peak detector
    for ( i=0 ; i<BUFFER_SIZE-HAMMING_SIZE-MA4_SIZE-2 ;i++){
        s= 0;
        for( k=i; k<i+ HAMMING_SIZE ;k++){
            s -= an_dx[k] *auw_hamm[k-i] ; 
                     }
        an_dx[i]= s/ (int32_t)1146; // divide by sum of auw_hamm 
    }

 
    n_th1=0; // threshold calculation
    for ( k=0 ; k<BUFFER_SIZE-HAMMING_SIZE ;k++){
        n_th1 += ((an_dx[k]>0)? an_dx[k] : ((int32_t)0-an_dx[k])) ;
    }
    n_th1= n_th1/ ( BUFFER_SIZE-HAMMING_SIZE);
    // [improved] adaptive threshold: 1.5x mean absolute value to reject noise peaks
    n_th1 = n_th1 + (n_th1 >> 1); // n_th1 *= 1.5
    if (n_th1 < 30) n_th1 = 30;   // minimum threshold floor to avoid noise triggering
    // peak location is acutally index for sharpest location of raw signal since we flipped the signal
    // [improved] min_distance=25 (250ms@100Hz = 240BPM upper limit)
    // [fix] max_num_peaks: 5→15, 5秒窗口72BPM有6个心搏，5个峰会漏检导致HR偏高
    maxim_find_peaks( an_dx_peak_locs, &n_npks, an_dx, BUFFER_SIZE-HAMMING_SIZE, n_th1, 25, 15 );//peak_height, peak_distance, max_num_peaks 

    n_peak_interval_sum =0;
    if (n_npks>=2){
        // [fix] collect all valid peak intervals with IQR outlier rejection
        int32_t an_peak_intervals[15];
        int32_t n_valid_intervals = 0;
        for (k=1; k<n_npks; k++) {
            int32_t interval = an_dx_peak_locs[k]-an_dx_peak_locs[k -1];
            // reject physiologically impossible intervals: <25 (>240BPM) or >150 (<40BPM)
            if (interval >= 25 && interval <= 150) {
                an_peak_intervals[n_valid_intervals++] = interval;
            }
        }
        // [fix] IQR outlier rejection for HR intervals (same logic as SpO2 ratio)
        if (n_valid_intervals >= 4) {
            maxim_sort_ascend(an_peak_intervals, n_valid_intervals);
            int32_t q1 = an_peak_intervals[n_valid_intervals/4];
            int32_t q3 = an_peak_intervals[n_valid_intervals*3/4];
            int32_t iqr = q3 - q1;
            int32_t lower = q1 - (iqr + (iqr >> 1)); // q1 - 1.5*IQR
            int32_t upper = q3 + (iqr + (iqr >> 1)); // q3 + 1.5*IQR
            int32_t n_filtered = 0;
            for (k=0; k<n_valid_intervals; k++) {
                if (an_peak_intervals[k] >= lower && an_peak_intervals[k] <= upper) {
                    an_peak_intervals[n_filtered++] = an_peak_intervals[k];
                }
            }
            if (n_filtered >= 1) n_valid_intervals = n_filtered;
        }
        if (n_valid_intervals >= 1) {
            // sort intervals and take median for robustness
            maxim_sort_ascend(an_peak_intervals, n_valid_intervals);
            if (n_valid_intervals >= 3) {
                n_peak_interval_sum = an_peak_intervals[n_valid_intervals/2]; // median
            } else {
                n_peak_interval_sum = 0;
                for (k=0; k<n_valid_intervals; k++)
                    n_peak_interval_sum += an_peak_intervals[k];
                n_peak_interval_sum = n_peak_interval_sum / n_valid_intervals;
            }
            *pn_heart_rate=(int32_t)(6000/n_peak_interval_sum);// beats per minutes
            // [improved] physiological range validation: 40~200 BPM
            if (*pn_heart_rate >= 40 && *pn_heart_rate <= 200)
                *pch_hr_valid  = 1;
            else {
                *pn_heart_rate = -999;
                *pch_hr_valid  = 0;
            }
        } else {
            *pn_heart_rate = -999;
            *pch_hr_valid  = 0;
        }
    }
    else  {
        *pn_heart_rate = -999;
        *pch_hr_valid  = 0;
    }
            
    for ( k=0 ; k<n_npks ;k++)
        an_ir_valley_locs[k]=an_dx_peak_locs[k]+HAMMING_SIZE/2; 


    // raw value : RED(=y) and IR(=X)
    // [VS-LMS fix] Use ORIGINAL (unfiltered) IR for SpO2 to preserve AC/DC ratio
    // VS-LMS filtered IR is only used for peak detection (HR)
    for (k=0 ; k<n_ir_buffer_length ; k++ )  {
        an_x[k] =  aun_ir_original[k] ; 
        an_y[k] =  pun_red_buffer[k] ; 
    }

    // find precise min near an_ir_valley_locs
    n_exact_ir_valley_locs_count =0; 
    for(k=0 ; k<n_npks ;k++){
        un_only_once =1;
        m=an_ir_valley_locs[k];
        n_c_min= 16777216;//2^24;
        if (m+5 <  BUFFER_SIZE-HAMMING_SIZE  && m-5 >0){
            for(i= m-5;i<m+5; i++)
                if (an_x[i]<n_c_min){
                    if (un_only_once >0){
                       un_only_once =0;
                   } 
                   n_c_min= an_x[i] ;
                   an_exact_ir_valley_locs[k]=i;
                }
            if (un_only_once ==0)
                n_exact_ir_valley_locs_count ++ ;
        }
    }
    if (n_exact_ir_valley_locs_count <2 ){
       *pn_spo2 =  -999 ; // do not use SPO2 since signal ratio is out of range
       *pch_spo2_valid  = 0; 
       return;
    }
    // 4 pt MA
    for(k=0; k< BUFFER_SIZE-MA4_SIZE; k++){
        an_x[k]=( an_x[k]+an_x[k+1]+ an_x[k+2]+ an_x[k+3])/(int32_t)4;
        an_y[k]=( an_y[k]+an_y[k+1]+ an_y[k+2]+ an_y[k+3])/(int32_t)4;
    }

    //using an_exact_ir_valley_locs , find ir-red DC andir-red AC for SPO2 calibration ratio
    //finding AC/DC maximum of raw ir * red between two valley locations
    n_ratio_average =0; 
    n_i_ratio_count =0; 
    
    for(k=0; k< 5; k++) an_ratio[k]=0;
    for (k=0; k< n_exact_ir_valley_locs_count; k++){
        if (an_exact_ir_valley_locs[k] > BUFFER_SIZE ){             
            *pn_spo2 =  -999 ; // do not use SPO2 since valley loc is out of range
            *pch_spo2_valid  = 0; 
            return;
        }
    }
    // find max between two valley locations 
    // and use ratio betwen AC compoent of Ir & Red and DC compoent of Ir & Red for SPO2 

    for (k=0; k< n_exact_ir_valley_locs_count-1; k++){
        n_y_dc_max= -16777216 ; 
        n_x_dc_max= - 16777216; 
        if (an_exact_ir_valley_locs[k+1]-an_exact_ir_valley_locs[k] >10){
            for (i=an_exact_ir_valley_locs[k]; i< an_exact_ir_valley_locs[k+1]; i++){
                if (an_x[i]> n_x_dc_max) {n_x_dc_max =an_x[i];n_x_dc_max_idx =i; }
                if (an_y[i]> n_y_dc_max) {n_y_dc_max =an_y[i];n_y_dc_max_idx=i;}
            }
            n_y_ac= (an_y[an_exact_ir_valley_locs[k+1]] - an_y[an_exact_ir_valley_locs[k] ] )*(n_y_dc_max_idx -an_exact_ir_valley_locs[k]); //red
            n_y_ac=  an_y[an_exact_ir_valley_locs[k]] + n_y_ac/ (an_exact_ir_valley_locs[k+1] - an_exact_ir_valley_locs[k])  ; 
        
        
            n_y_ac=  an_y[n_y_dc_max_idx] - n_y_ac;    // subracting linear DC compoenents from raw 
            n_x_ac= (an_x[an_exact_ir_valley_locs[k+1]] - an_x[an_exact_ir_valley_locs[k] ] )*(n_x_dc_max_idx -an_exact_ir_valley_locs[k]); // ir
            n_x_ac=  an_x[an_exact_ir_valley_locs[k]] + n_x_ac/ (an_exact_ir_valley_locs[k+1] - an_exact_ir_valley_locs[k]); 
            n_x_ac=  an_x[n_y_dc_max_idx] - n_x_ac;      // subracting linear DC compoenents from raw 
            n_nume=( n_y_ac *n_x_dc_max)>>7 ; //prepare X100 to preserve floating value
            n_denom= ( n_x_ac *n_y_dc_max)>>7;
            if (n_denom>0  && n_i_ratio_count <5 &&  n_nume != 0)
            {   
                an_ratio[n_i_ratio_count]= (n_nume*20)/n_denom ; //formular is ( n_y_ac *n_x_dc_max) / ( n_x_ac *n_y_dc_max) ;  ///*************************n_nume原来是*100************************//
                n_i_ratio_count++;
            }
        }
    }

    maxim_sort_ascend(an_ratio, n_i_ratio_count);

    // [improved] IQR-based outlier rejection before taking median
    if (n_i_ratio_count >= 4) {
        int32_t n_q1 = an_ratio[n_i_ratio_count / 4];
        int32_t n_q3 = an_ratio[(n_i_ratio_count * 3) / 4];
        int32_t n_iqr = n_q3 - n_q1;
        int32_t n_lower = n_q1 - (n_iqr + (n_iqr >> 1)); // Q1 - 1.5*IQR
        int32_t n_upper = n_q3 + (n_iqr + (n_iqr >> 1)); // Q3 + 1.5*IQR
        // compact valid ratios in-place
        int32_t n_valid = 0;
        for (k = 0; k < n_i_ratio_count; k++) {
            if (an_ratio[k] >= n_lower && an_ratio[k] <= n_upper) {
                an_ratio[n_valid++] = an_ratio[k];
            }
        }
        if (n_valid > 0) n_i_ratio_count = n_valid;
    }

    n_middle_idx= n_i_ratio_count/2;

    if (n_middle_idx >1)
        n_ratio_average =( an_ratio[n_middle_idx-1] +an_ratio[n_middle_idx])/2; // use median
    else
        n_ratio_average = an_ratio[n_middle_idx ];

    if( n_ratio_average>2 && n_ratio_average <184){
        n_spo2_calc= uch_spo2_table[n_ratio_average] ;
        *pn_spo2 = n_spo2_calc ;
        *pch_spo2_valid  = 1;//  float_SPO2 =  -45.060*n_ratio_average* n_ratio_average/10000 + 30.354 *n_ratio_average/100 + 94.845 ;  // for comparison with table
    }
    else{
        *pn_spo2 =  -999 ; // do not use SPO2 since signal ratio is out of range
        *pch_spo2_valid  = 0; 
    }
}


void maxim_find_peaks(int32_t *pn_locs, int32_t *pn_npks, int32_t *pn_x, int32_t n_size, int32_t n_min_height, int32_t n_min_distance, int32_t n_max_num)
/**
* \brief        Find peaks
* \par          Details
*               Find at most MAX_NUM peaks above MIN_HEIGHT separated by at least MIN_DISTANCE
*
* \retval       None
*/
{
    maxim_peaks_above_min_height( pn_locs, pn_npks, pn_x, n_size, n_min_height );
    maxim_remove_close_peaks( pn_locs, pn_npks, pn_x, n_min_distance );
    *pn_npks = min( *pn_npks, n_max_num );
}

void maxim_peaks_above_min_height(int32_t *pn_locs, int32_t *pn_npks, int32_t  *pn_x, int32_t n_size, int32_t n_min_height)
/**
* \brief        Find peaks above n_min_height
* \par          Details
*               Find all peaks above MIN_HEIGHT
*
* \retval       None
*/
{
    int32_t i = 1, n_width;
    *pn_npks = 0;
    
    while (i < n_size-1){
        if (pn_x[i] > n_min_height && pn_x[i] > pn_x[i-1]){            // find left edge of potential peaks
            n_width = 1;
            while (i+n_width < n_size && pn_x[i] == pn_x[i+n_width])    // find flat peaks
                n_width++;
            if (pn_x[i] > pn_x[i+n_width] && (*pn_npks) < 15 ){                            // find right edge of peaks
                pn_locs[(*pn_npks)++] = i;        
                // for flat peaks, peak location is left edge
                i += n_width+1;
            }
            else
                i += n_width;
        }
        else
            i++;
    }
}


void maxim_remove_close_peaks(int32_t *pn_locs, int32_t *pn_npks, int32_t *pn_x, int32_t n_min_distance)
/**
* \brief        Remove peaks
* \par          Details
*               Remove peaks separated by less than MIN_DISTANCE
*
* \retval       None
*/
{
    
    int32_t i, j, n_old_npks, n_dist;
    
    /* Order peaks from large to small */
    maxim_sort_indices_descend( pn_x, pn_locs, *pn_npks );

    for ( i = -1; i < *pn_npks; i++ ){
        n_old_npks = *pn_npks;
        *pn_npks = i+1;
        for ( j = i+1; j < n_old_npks; j++ ){
            n_dist =  pn_locs[j] - ( i == -1 ? -1 : pn_locs[i] ); // lag-zero peak of autocorr is at index -1
            if ( n_dist > n_min_distance || n_dist < -n_min_distance )
                pn_locs[(*pn_npks)++] = pn_locs[j];
        }
    }

    // Resort indices longo ascending order
    maxim_sort_ascend( pn_locs, *pn_npks );
}

void maxim_sort_ascend(int32_t *pn_x,int32_t n_size) 
/**
* \brief        Sort array
* \par          Details
*               Sort array in ascending order (insertion sort algorithm)
*
* \retval       None
*/
{
    int32_t i, j, n_temp;
    for (i = 1; i < n_size; i++) {
        n_temp = pn_x[i];
        for (j = i; j > 0 && n_temp < pn_x[j-1]; j--)
            pn_x[j] = pn_x[j-1];
        pn_x[j] = n_temp;
    }
}

void maxim_sort_indices_descend(int32_t *pn_x, int32_t *pn_indx, int32_t n_size)
/**
* \brief        Sort indices
* \par          Details
*               Sort indices according to descending order (insertion sort algorithm)
*
* \retval       None
*/ 
{
    int32_t i, j, n_temp;
    for (i = 1; i < n_size; i++) {
        n_temp = pn_indx[i];
        for (j = i; j > 0 && pn_x[n_temp] > pn_x[pn_indx[j-1]]; j--)
            pn_indx[j] = pn_indx[j-1];
        pn_indx[j] = n_temp;
    }
}

