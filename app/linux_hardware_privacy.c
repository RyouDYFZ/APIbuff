#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

static int (*real_open)(const char *pathname, int flags, mode_t mode) = NULL;
static int (*real_open64)(const char *pathname, int flags, mode_t mode) = NULL;
static int (*real_openat)(int dirfd, const char *pathname, int flags, mode_t mode) = NULL;
static int (*real_openat64)(int dirfd, const char *pathname, int flags, mode_t mode) = NULL;
static int (*real_uname)(struct utsname *buf) = NULL;
static int (*real_gethostname)(char *name, size_t len) = NULL;

static void init_hooks(void) {
    if (!real_open) {
        real_open = (int (*)(const char *, int, mode_t))dlsym(RTLD_NEXT, "open");
        real_open64 = (int (*)(const char *, int, mode_t))dlsym(RTLD_NEXT, "open64");
        real_openat = (int (*)(int, const char *, int, mode_t))dlsym(RTLD_NEXT, "openat");
        real_openat64 = (int (*)(int, const char *, int, mode_t))dlsym(RTLD_NEXT, "openat64");
        real_uname = (int (*)(struct utsname *))dlsym(RTLD_NEXT, "uname");
        real_gethostname = (int (*)(char *, size_t))dlsym(RTLD_NEXT, "gethostname");
    }
}

static const char *get_replacement(const char *path) {
    if (!path) return NULL;
    const char *synth_dir = getenv("FREEBUFF_SYNTHETIC_DIR");
    if (!synth_dir) return NULL;
    static __thread char redirect_buf[1024];

    if (strstr(path, "/etc/machine-id") != NULL || strstr(path, "/var/lib/dbus/machine-id") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/machine-id", synth_dir);
        return redirect_buf;
    }
    if (strstr(path, "product_uuid") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/product_uuid", synth_dir);
        return redirect_buf;
    }
    if (strstr(path, "product_serial") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/product_serial", synth_dir);
        return redirect_buf;
    }
    if (strstr(path, "product_name") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/product_name", synth_dir);
        return redirect_buf;
    }
    if (strstr(path, "sys_vendor") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/sys_vendor", synth_dir);
        return redirect_buf;
    }
    if (strstr(path, "product_version") != NULL) {
        snprintf(redirect_buf, sizeof(redirect_buf), "%s/synthetic/product_version", synth_dir);
        return redirect_buf;
    }
    return NULL;
}

int open(const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    const char *rep = get_replacement(pathname);
    return real_open ? real_open(rep ? rep : pathname, flags, mode) : -1;
}

int open64(const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    const char *rep = get_replacement(pathname);
    return real_open64 ? real_open64(rep ? rep : pathname, flags, mode) : (real_open ? real_open(rep ? rep : pathname, flags, mode) : -1);
}

int openat(int dirfd, const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    const char *rep = get_replacement(pathname);
    if (rep) {
        return real_openat ? real_openat(AT_FDCWD, rep, flags, mode) : -1;
    }
    return real_openat ? real_openat(dirfd, pathname, flags, mode) : -1;
}

int openat64(int dirfd, const char *pathname, int flags, ...) {
    init_hooks();
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    const char *rep = get_replacement(pathname);
    if (rep) {
        return real_openat64 ? real_openat64(AT_FDCWD, rep, flags, mode) : (real_openat ? real_openat(AT_FDCWD, rep, flags, mode) : -1);
    }
    return real_openat64 ? real_openat64(dirfd, pathname, flags, mode) : (real_openat ? real_openat(dirfd, pathname, flags, mode) : -1);
}

int uname(struct utsname *buf) {
    init_hooks();
    int ret = real_uname ? real_uname(buf) : 0;
    if (ret == 0 && buf) {
        const char *host = getenv("FREEBUFF_SYNTHETIC_HOSTNAME");
        if (host && *host) {
            strncpy(buf->nodename, host, sizeof(buf->nodename) - 1);
            buf->nodename[sizeof(buf->nodename) - 1] = '\0';
        }
    }
    return ret;
}

int gethostname(char *name, size_t len) {
    const char *host = getenv("FREEBUFF_SYNTHETIC_HOSTNAME");
    if (host && *host) {
        strncpy(name, host, len - 1);
        name[len - 1] = '\0';
        return 0;
    }
    init_hooks();
    return real_gethostname ? real_gethostname(name, len) : -1;
}
