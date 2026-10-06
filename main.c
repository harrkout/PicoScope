#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/adc.h"
#include "hardware/dma.h"
#include "hardware/pwm.h"

// Samples captured per ADC channel, and the interleaved (CH1,CH2,CH1,CH2,...)
// total buffer size that scope_gui.py expects to read back.
#define SAMPLES_PER_CHAN 2048
#define CAPTURE_DEPTH (SAMPLES_PER_CHAN * 2)
uint16_t capture_buf[CAPTURE_DEPTH];

int main() {
    stdio_init_all();

    // Disable CRLF translation explicitly
    stdio_set_translate_crlf(NULL, false);

    // PWM test signal on GPIO15 (~1 kHz, 50% duty cycle)
    gpio_set_function(15, GPIO_FUNC_PWM);
    uint slice_num = pwm_gpio_to_slice_num(15);
    pwm_set_wrap(slice_num, 47999); // ~1 kHz test signal
    pwm_set_chan_level(slice_num, PWM_CHAN_A, 24000); // 50% duty cycle
    pwm_set_enabled(slice_num, true);

    // ADC setup: CH1 = GPIO26 (ADC0), CH2 = GPIO27 (ADC1)
    adc_gpio_init(26);
    adc_gpio_init(27);
    adc_init();
    adc_select_input(0);           // round robin must start on ADC0
    adc_set_round_robin(0b0011);   // round robin between ADC0 and ADC1
    adc_fifo_setup(true, true, 1, false, false);

    // Overall ADC sample rate = 1 MS/s, so round-robin gives 500 kS/s per channel
    adc_set_clkdiv(47);

    // Setup DMA channel
    int dma_chan = dma_claim_unused_channel(true);
    dma_channel_config c = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_16);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_dreq(&c, DREQ_ADC);

    while (true) {
        int c_in = getchar_timeout_us(0);
        if (c_in == 's') {
            adc_fifo_drain();
            adc_select_input(0); // ensure buffer always starts CH1,CH2,CH1,CH2,...
            dma_channel_configure(dma_chan, &c, capture_buf, &adc_hw->fifo, CAPTURE_DEPTH, true);
            adc_run(true);
            
            dma_channel_wait_for_finish_blocking(dma_chan);
            adc_run(false);

            // Write raw binary memory buffer and flush
            fwrite(capture_buf, sizeof(uint16_t), CAPTURE_DEPTH, stdout);
            fflush(stdout);
        }
    }
}
