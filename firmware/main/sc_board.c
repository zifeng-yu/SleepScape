#include "sc_board.h"
#include <string.h>
#include <math.h>
#include "esp_timer.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_pdm.h"
#include "driver/rmt_tx.h"
#include "esp_attr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static i2c_master_dev_handle_t aht, light;
static i2s_chan_handle_t microphone;
static volatile uint32_t overflows;
static sc_veml_t light_sensor;

static bool IRAM_ATTR audio_overflow(i2s_chan_handle_t handle,
                                    i2s_event_data_t *event, void *context) {
    (void)handle; (void)event; (void)context;
    ++overflows;
    return false;
}

static esp_err_t sensor_bus_init(void) {
    i2c_master_bus_handle_t bus;
    i2c_master_bus_config_t bus_config = {
        .i2c_port = 0, .sda_io_num = SC_SENSOR_SDA, .scl_io_num = SC_SENSOR_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = false,
    };
    esp_err_t result = i2c_new_master_bus(&bus_config, &bus);
    if (result != ESP_OK) return result;
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = SC_AHT_ADDRESS,
        .scl_speed_hz = SC_SENSOR_I2C_HZ,
    };
    result = i2c_master_bus_add_device(bus, &config, &aht);
    config.device_address = SC_VEML_ADDRESS;
    esp_err_t light_result = i2c_master_bus_add_device(bus, &config, &light);
    return result != ESP_OK ? result : light_result;
}

static void quiet_outputs(void) {
    /* Speaker BCLK/DATA remain low; no TX audio driver is installed. */
    gpio_set_direction(GPIO_NUM_19, GPIO_MODE_OUTPUT);
    gpio_set_direction(GPIO_NUM_22, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_19, 0);
    gpio_set_level(GPIO_NUM_22, 0);
    /* Explicitly send black to the SK6812, instead of leaving a stale color. */
    rmt_channel_handle_t channel;
    rmt_encoder_handle_t encoder;
    rmt_tx_channel_config_t config = {
        .gpio_num = GPIO_NUM_27, .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10000000, .mem_block_symbols = 64,
        .trans_queue_depth = 1,
    };
    if (rmt_new_tx_channel(&config, &channel) != ESP_OK) return;
    rmt_copy_encoder_config_t copy = {};
    if (rmt_new_copy_encoder(&copy, &encoder) != ESP_OK) {
        rmt_del_channel(channel); return;
    }
    rmt_symbol_word_t black[25] = {0};
    for (size_t i = 0; i < 24; ++i) {
        black[i].level0 = 1; black[i].duration0 = 3;
        black[i].level1 = 0; black[i].duration1 = 9;
    }
    black[24].duration0 = 800; black[24].duration1 = 800;
    rmt_transmit_config_t transmit = {.loop_count = 0};
    if (rmt_enable(channel) == ESP_OK) {
        if (rmt_transmit(channel, encoder, black, sizeof(black), &transmit) == ESP_OK)
            rmt_tx_wait_all_done(channel, 100);
        rmt_disable(channel);
    }
    rmt_del_encoder(encoder); rmt_del_channel(channel);
    gpio_set_direction(GPIO_NUM_27, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_27, 0);
}

esp_err_t sc_board_init(void) {
    quiet_outputs();
    sc_veml_reset(&light_sensor);
    esp_err_t bus_result = sensor_bus_init();
    /* I2C failures do not prevent independent microphone initialization. */
    esp_err_t result;
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel.dma_desc_num = 12;
    channel.dma_frame_num = 256;
    result = i2s_new_channel(&channel, NULL, &microphone);
    if (result != ESP_OK) return result;
    i2s_pdm_rx_config_t pdm = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                         I2S_SLOT_MODE_MONO),
        .gpio_cfg = {.clk = SC_MIC_CLK, .din = SC_MIC_DATA, .invert_flags.clk_inv = false},
    };
    /* 16000 * 128 = 2.048 MHz, within SPM1423's 1--3.25 MHz specification. */
    pdm.clk_cfg.dn_sample_mode = I2S_PDM_DSR_16S;
    pdm.slot_cfg.slot_mask = I2S_PDM_SLOT_RIGHT;
    result = i2s_channel_init_pdm_rx_mode(microphone, &pdm);
    if (result != ESP_OK) return result;
    i2s_event_callbacks_t callbacks = {.on_recv_q_ovf = audio_overflow};
    result = i2s_channel_register_event_callback(microphone, &callbacks, NULL);
    if (result != ESP_OK) return result;
    result = i2s_channel_enable(microphone);
    return result != ESP_OK ? result : bus_result;
}

static uint8_t aht_crc(const uint8_t *bytes, size_t count) {
    uint8_t crc = 0xff;
    for (size_t i = 0; i < count; ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
    return crc;
}

bool sc_board_temperature(float *temperature, float *humidity) {
    if (!aht) return false;
    const uint8_t status_command = 0x71;
    uint8_t status = 0;
    if (i2c_master_transmit_receive(aht, &status_command, 1, &status, 1, 50) != ESP_OK)
        return false;
    if (!(status & 0x08)) {
        const uint8_t initialize[] = {0xbe, 0x08, 0x00};
        if (i2c_master_transmit(aht, initialize, sizeof(initialize), 50) != ESP_OK)
            return false;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    const uint8_t trigger[] = {0xac, 0x33, 0x00};
    if (i2c_master_transmit(aht, trigger, sizeof(trigger), 50) != ESP_OK) return false;
    vTaskDelay(pdMS_TO_TICKS(85));
    uint8_t bytes[7];
    if (i2c_master_receive(aht, bytes, sizeof(bytes), 50) != ESP_OK ||
        (bytes[0] & 0x80) || !(bytes[0] & 0x08) || aht_crc(bytes, 6) != bytes[6])
        return false;
    uint32_t rh = ((uint32_t)bytes[1] << 12) | ((uint32_t)bytes[2] << 4) | (bytes[3] >> 4);
    uint32_t temp = ((uint32_t)(bytes[3] & 15) << 16) | ((uint32_t)bytes[4] << 8) | bytes[5];
    *humidity = (float)rh * 100.0f / 1048576.0f;
    *temperature = (float)temp * 200.0f / 1048576.0f - 50.0f;
    return *humidity >= 0 && *humidity <= 100 && *temperature >= -40 && *temperature <= 85;
}

static bool light_read(void *context, uint8_t reg, uint8_t bytes[2]) {
    (void)context;
    return light && i2c_master_transmit_receive(light, &reg, 1, bytes, 2, 50) == ESP_OK;
}
static bool light_write(void *context, uint8_t reg, const uint8_t bytes[2]) {
    (void)context;
    const uint8_t command[] = {reg, bytes[0], bytes[1]};
    return light && i2c_master_transmit(light, command, sizeof(command), 50) == ESP_OK;
}
static uint64_t light_now(void *context) {
    (void)context;
    return (uint64_t)esp_timer_get_time() / 1000;
}
bool sc_board_light(float *lux, uint32_t *quality) {
    const sc_veml_io_t io = {.read_word = light_read, .write_word = light_write,
                            .now_ms = light_now};
    sc_veml_sample_t sample;
    bool valid = sc_veml_poll(&light_sensor, &io, &sample);
    *lux = sample.lux; *quality = sample.quality;
    return valid;
}

esp_err_t sc_board_audio_read(int16_t *samples, size_t capacity, size_t *count) {
    *count = 0;
    if (!microphone) return ESP_ERR_INVALID_STATE;
    size_t bytes = 0;
    esp_err_t result = i2s_channel_read(microphone, samples, capacity * sizeof(*samples),
                                       &bytes, 100);
    *count = bytes / sizeof(*samples);
    return result;
}
uint32_t sc_board_audio_overflows(void) { return overflows; }
