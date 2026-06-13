#ifndef SENSOR_ENV_H
#define SENSOR_ENV_H

void env_init();
void env_update();
float get_temp();
float get_humi();
bool env_is_valid(); // Trả về true khi đã có ít nhất 1 lần đọc thành công

#endif