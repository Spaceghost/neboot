/* Minimal Linux initramfs witness. It prints only from PID 1. */
.equ SYS_write, 64
.equ SYS_exit, 93
.equ SYS_reboot, 142
.equ SYS_getpid, 172

.text
.globl _start
.type _start, %function
_start:
  mov x8, #SYS_getpid
  svc #0
  cmp x0, #1
  b.ne .Lfail

  mov x0, #1
  adr x1, .Lmarker
  mov x2, #.Lmarker_end - .Lmarker
  mov x8, #SYS_write
  svc #0
  cmp x0, #.Lmarker_end - .Lmarker
  b.ne .Lfail

  movz w0, #0xdead
  movk w0, #0xfee1, lsl #16
  movz w1, #0x1969
  movk w1, #0x2812, lsl #16
  movz w2, #0xfedc
  movk w2, #0x4321, lsl #16
  mov x3, #0
  mov x8, #SYS_reboot
  svc #0
.Lfail:
  mov x0, #1
  mov x8, #SYS_exit
  svc #0
  brk #0
.size _start, .-_start

.Lmarker:
  .ascii "NEBOOT_ARM64_PID1=1\n"
.Lmarker_end:
.section .note.GNU-stack,"",%progbits
