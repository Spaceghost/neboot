NELUA ?= nelua
NECC ?= necc
NELINKER ?= nelinker
OUT ?= build
ESP ?= $(OUT)/esp
NATIVE_OUT ?= $(OUT)/native

.PHONY: all native stage stage-native stage-iso stage-iso-native run-live run-live-native
.DELETE_ON_ERROR:
.SECONDARY:
all: $(OUT)/BOOTX64.EFI $(OUT)/second.efi
native: $(NATIVE_OUT)/BOOTX64.EFI $(NATIVE_OUT)/second.efi

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

$(NATIVE_OUT)/%.o: src/%.nelua src/efi.nelua Makefile | $(NATIVE_OUT)
	$(NECC) --native --release -L src -c "$<" -o "$@"

$(NATIVE_OUT)/BOOTX64.EFI: $(NATIVE_OUT)/menu.o Makefile
	$(NELINKER) --efi-app --image-base 0x100000000 -e efi_main -o "$@" "$<"

$(NATIVE_OUT)/second.efi: $(NATIVE_OUT)/chain.o
	$(NELINKER) --efi-app -e efi_main -o "$@" "$<"

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
