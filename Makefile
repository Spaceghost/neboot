NELUA ?= nelua
NECC ?= necc
NELINKER ?= nelinker
OUT ?= build
ESP ?= $(OUT)/esp
NATIVE_OUT ?= $(OUT)/native
CORE_NATIVE_OUT ?= $(OUT)/core-native

.PHONY: all native native-core stage stage-native stage-core-native stage-iso stage-iso-native run-live run-live-native
.DELETE_ON_ERROR:
.SECONDARY:
all: $(OUT)/BOOTX64.EFI $(OUT)/second.efi
native: $(NATIVE_OUT)/BOOTX64.EFI $(NATIVE_OUT)/second.efi

# Core uses Theseus as PID 1 directly; it has no OpenRC recovery menu entry.
native-core: $(CORE_NATIVE_OUT)/BOOTX64.EFI $(NATIVE_OUT)/second.efi

$(OUT):
	mkdir -p "$@"

$(OUT)/%.c: src/%.nelua src/efi.nelua | $(OUT)
	$(NELUA) -L src --print-code "$<" > "$@"

$(OUT)/%.o: $(OUT)/%.c
	$(NECC) -c "$<" -o "$@"

$(OUT)/BOOTX64.EFI: $(OUT)/menu.o Makefile
	$(NELINKER) --efi-app --image-base 0x100000000 -e efi_main -o "$@" "$<"

$(OUT)/second.efi: $(OUT)/chain.o
	$(NELINKER) --efi-app -e efi_main -o "$@" "$<"

$(NATIVE_OUT):
	mkdir -p "$@"

$(CORE_NATIVE_OUT):
	mkdir -p "$@"

$(NATIVE_OUT)/%.o: src/%.nelua src/efi.nelua Makefile | $(NATIVE_OUT)
	$(NECC) --native --release -L src -c "$<" -o "$@"

$(CORE_NATIVE_OUT)/menu.o: src/menu.nelua src/efi.nelua Makefile | $(CORE_NATIVE_OUT)
	$(NECC) --native --release -L src --define NEBOOT_CORE=true -c "$<" -o "$@"

$(NATIVE_OUT)/BOOTX64.EFI: $(NATIVE_OUT)/menu.o Makefile
	$(NELINKER) --efi-app --image-base 0x100000000 -e efi_main -o "$@" "$<"

$(NATIVE_OUT)/second.efi: $(NATIVE_OUT)/chain.o
	$(NELINKER) --efi-app -e efi_main -o "$@" "$<"

$(CORE_NATIVE_OUT)/BOOTX64.EFI: $(CORE_NATIVE_OUT)/menu.o Makefile
	$(NELINKER) --efi-app --image-base 0x100000000 -e efi_main -o "$@" "$<"

stage: all
	test -n "$(ESP)" && test -f "$(KERNEL)" && test -f "$(INITRD)"
	install -Dm644 "$(OUT)/BOOTX64.EFI" "$(ESP)/EFI/BOOT/BOOTX64.EFI"
	install -Dm644 "$(OUT)/second.efi" "$(ESP)/EFI/TOOLS/second.efi"
	install -Dm644 "$(KERNEL)" "$(ESP)/boot/vmlinuz"
	install -Dm644 "$(INITRD)" "$(ESP)/boot/initramfs"

stage-native: native
	test -n "$(ESP)" && test -f "$(KERNEL)" && test -f "$(INITRD)"
	install -Dm644 "$(NATIVE_OUT)/BOOTX64.EFI" "$(ESP)/EFI/BOOT/BOOTX64.EFI"
	install -Dm644 "$(NATIVE_OUT)/second.efi" "$(ESP)/EFI/TOOLS/second.efi"
	install -Dm644 "$(KERNEL)" "$(ESP)/boot/vmlinuz"
	install -Dm644 "$(INITRD)" "$(ESP)/boot/initramfs"

stage-core-native: native-core
	test -n "$(ESP)" && test -f "$(KERNEL)" && test -f "$(INITRD)"
	install -Dm644 "$(CORE_NATIVE_OUT)/BOOTX64.EFI" "$(ESP)/EFI/BOOT/BOOTX64.EFI"
	install -Dm644 "$(NATIVE_OUT)/second.efi" "$(ESP)/EFI/TOOLS/second.efi"
	install -Dm644 "$(KERNEL)" "$(ESP)/boot/vmlinuz"
	install -Dm644 "$(INITRD)" "$(ESP)/boot/initramfs"

# Extract the kernel and initramfs as ordinary files on the same volume as the loader.
stage-iso: all
	test -n "$(ESP)" && test -f "$(ISO)"
	mkdir -p "$(OUT)/live"
	xorriso -osirrox on -overwrite on -indev "$(ISO)" -extract /boot/vmlinuz "$(OUT)/live/vmlinuz" -extract /boot/initramfs "$(OUT)/live/initramfs"
	$(MAKE) stage KERNEL="$(OUT)/live/vmlinuz" INITRD="$(OUT)/live/initramfs"

stage-iso-native: native
	test -n "$(ESP)" && test -f "$(ISO)"
	mkdir -p "$(OUT)/live"
	xorriso -osirrox on -overwrite on -indev "$(ISO)" -extract /boot/vmlinuz "$(OUT)/live/vmlinuz" -extract /boot/initramfs "$(OUT)/live/initramfs"
	$(MAKE) stage-native KERNEL="$(OUT)/live/vmlinuz" INITRD="$(OUT)/live/initramfs"

# Resolve Cascadia at invocation so the boot volume gets the latest verified RAM root.
run-live: all
	iso="$(ISO)"; if [ -z "$$iso" ]; then iso=$$(cascadia-live --prepare) || exit; fi; \
	$(MAKE) stage-iso ISO="$$iso" && \
	python3 tests/smoke-ovmf.py --esp "$(ESP)" --interactive

run-live-native: native
	iso="$(ISO)"; if [ -z "$$iso" ]; then iso=$$(cascadia-live --prepare) || exit; fi; \
	$(MAKE) stage-iso-native ISO="$$iso" && \
	python3 tests/smoke-ovmf.py --esp "$(ESP)" --interactive

ARM64_OUT ?= $(OUT)/aarch64
CLANG ?= clang
LLD_LINK ?= lld-link

.PHONY: arm64
arm64: $(ARM64_OUT)/BOOTAA64.EFI $(ARM64_OUT)/second.efi

$(ARM64_OUT):
	mkdir -p "$@"

$(ARM64_OUT)/%.c: src/%.nelua src/efi.nelua Makefile | $(ARM64_OUT)
	$(NELUA) -L src --cc "$(CLANG)" --target aarch64-none-elf \
		--cflags=-ffreestanding --define NEBOOT_ARM64=true --print-code "$<" > "$@"

$(ARM64_OUT)/%.obj: $(ARM64_OUT)/%.c
	$(CLANG) --target=aarch64-windows-msvc -O2 -ffreestanding -fno-builtin \
		-fno-stack-protector -mno-stack-arg-probe -c "$<" -o "$@"

$(ARM64_OUT)/BOOTAA64.EFI: $(ARM64_OUT)/menu.obj Makefile
	$(LLD_LINK) /machine:arm64 /subsystem:efi_application /entry:efi_main \
		/nodefaultlib /base:0x100000000 /out:"$@" "$<"

$(ARM64_OUT)/second.efi: $(ARM64_OUT)/chain.obj Makefile
	$(LLD_LINK) /machine:arm64 /subsystem:efi_application /entry:efi_main \
		/nodefaultlib /out:"$@" "$<"

ARM64_ESP ?= $(ARM64_OUT)/esp

.PHONY: stage-arm64 smoke-arm64
stage-arm64: arm64
	install -Dm644 "$(ARM64_OUT)/BOOTAA64.EFI" "$(ARM64_ESP)/EFI/BOOT/BOOTAA64.EFI"
	install -Dm644 "$(ARM64_OUT)/second.efi" "$(ARM64_ESP)/EFI/TOOLS/second.efi"

smoke-arm64: stage-arm64
	python3 tests/smoke-ovmf.py --arm64-menu --esp "$(ARM64_ESP)" --logs "$(ARM64_OUT)/logs"

ARM64_LINUX_TEST_ESP ?= $(ARM64_OUT)/linux-test-esp

.PHONY: stage-arm64-linux smoke-arm64-linux
stage-arm64-linux: stage-arm64
	@test -n "$(KERNEL)" && test -f "$(KERNEL)"
	@test -n "$(INITRD)" && test -f "$(INITRD)"
	install -Dm644 "$(KERNEL)" "$(ARM64_ESP)/boot/vmlinuz"
	install -Dm644 "$(INITRD)" "$(ARM64_ESP)/boot/initramfs"

$(ARM64_OUT)/test-init: tests/arm64-init.S Makefile | $(ARM64_OUT)
	$(CLANG) --target=aarch64-none-elf -fuse-ld=lld -nostdlib -static \
		-Wl,-e,_start "$<" -o "$@"

$(ARM64_OUT)/test-initramfs: $(ARM64_OUT)/test-init tests/arm64-initramfs.py
	python3 tests/arm64-initramfs.py "$<" "$@"

smoke-arm64-linux: arm64 $(ARM64_OUT)/test-initramfs
	$(MAKE) stage-arm64-linux ARM64_ESP="$(ARM64_LINUX_TEST_ESP)" \
		KERNEL="$(KERNEL)" INITRD="$(ARM64_OUT)/test-initramfs"
	python3 tests/smoke-ovmf.py --arm64-linux --esp "$(ARM64_LINUX_TEST_ESP)" \
		--logs "$(ARM64_OUT)/logs"

ARM64_NELINKER_OUT ?= $(OUT)/aarch64-nelinker
ARM64_NELINKER_ESP ?= $(ARM64_NELINKER_OUT)/esp
ARM64_NELINKER_TEST_ESP ?= $(ARM64_NELINKER_OUT)/linux-test-esp

.PHONY: arm64-nelinker stage-arm64-nelinker smoke-arm64-nelinker smoke-arm64-nelinker-linux
arm64-nelinker: $(ARM64_NELINKER_OUT)/BOOTAA64.EFI $(ARM64_NELINKER_OUT)/second.efi

$(ARM64_NELINKER_OUT):
	mkdir -p "$@"

$(ARM64_NELINKER_OUT)/%.o: $(ARM64_OUT)/%.c Makefile | $(ARM64_NELINKER_OUT)
	$(CLANG) --target=aarch64-none-elf -O2 -ffreestanding -fno-builtin \
		-fno-stack-protector -fno-pic -fno-unwind-tables -fno-asynchronous-unwind-tables \
		-c "$<" -o "$@"

$(ARM64_NELINKER_OUT)/BOOTAA64.EFI: $(ARM64_NELINKER_OUT)/menu.o Makefile
	$(NELINKER) --aarch64 --efi-app --image-base 0x100000000 -o "$@" "$<"

$(ARM64_NELINKER_OUT)/second.efi: $(ARM64_NELINKER_OUT)/chain.o Makefile
	$(NELINKER) --aarch64 --efi-app -o "$@" "$<"

stage-arm64-nelinker: arm64-nelinker
	install -Dm644 "$(ARM64_NELINKER_OUT)/BOOTAA64.EFI" "$(ARM64_NELINKER_ESP)/EFI/BOOT/BOOTAA64.EFI"
	install -Dm644 "$(ARM64_NELINKER_OUT)/second.efi" "$(ARM64_NELINKER_ESP)/EFI/TOOLS/second.efi"

smoke-arm64-nelinker: stage-arm64-nelinker
	python3 tests/smoke-ovmf.py --arm64-menu --esp "$(ARM64_NELINKER_ESP)" \
		--logs "$(ARM64_NELINKER_OUT)/logs"

smoke-arm64-nelinker-linux: arm64-nelinker $(ARM64_OUT)/test-initramfs
	@test -n "$(KERNEL)" && test -f "$(KERNEL)"
	$(MAKE) stage-arm64-nelinker ARM64_NELINKER_ESP="$(ARM64_NELINKER_TEST_ESP)"
	install -Dm644 "$(KERNEL)" "$(ARM64_NELINKER_TEST_ESP)/boot/vmlinuz"
	install -Dm644 "$(ARM64_OUT)/test-initramfs" "$(ARM64_NELINKER_TEST_ESP)/boot/initramfs"
	python3 tests/smoke-ovmf.py --arm64-linux --esp "$(ARM64_NELINKER_TEST_ESP)" \
		--logs "$(ARM64_NELINKER_OUT)/logs"
