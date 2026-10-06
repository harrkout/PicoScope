cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

set(PICO_BOARD pico2 CACHE STRING "Board type")

project(pico_scope C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(pico_scope main.c)

# Enable USB CDC, disable UART
pico_enable_stdio_usb(pico_scope 1)
pico_enable_stdio_uart(pico_scope 0)

# Disable automatic CR/LF translation on stdio
target_compile_definitions(pico_scope PRIVATE
    PICO_STDIO_DEFAULT_CRLF=0
)

target_link_libraries(pico_scope 
    pico_stdlib 
    hardware_adc 
    hardware_dma
    hardware_pwm
)

pico_add_extra_outputs(pico_scope)
