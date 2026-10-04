#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <grp.h>
#include <stddef.h>
#include <sys/types.h>
#include <unistd.h>

/* Build on a newer host without requiring glibc 2.34 in bullseye images.
 * dlsym moved into libc in 2.34; bind the original x86-64 ABI explicitly and
 * retain libdl.so.2 in DT_NEEDED (see the bundle build command).
 */
#if defined(__GLIBC__) && defined(__x86_64__)
extern void *rsi_legacy_dlsym(void *, const char *);
__asm__(".symver rsi_legacy_dlsym,dlsym@GLIBC_2.2.5");
#define dlsym rsi_legacy_dlsym
#endif

/*
 * Compatibility shim for a user namespace that maps only container uid/gid 0.
 * Package managers legitimately chown files and drop privileges to system
 * accounts; those operations return EPERM/EINVAL in this environment.  Keep
 * the files/process root-owned while reporting success.  The shim is loaded
 * only by the apt/dpkg wrappers, never by benchmark commands in general.
 */

#define RSI_WRAP_FAIL_OK(name, args, callargs)                 \
    int name args {                                            \
        static int (*real_fn) args;                            \
        if (real_fn == NULL)                                   \
            real_fn = dlsym(RTLD_NEXT, #name);                 \
        int rc = real_fn callargs;                             \
        if (rc == -1 && (errno == EPERM || errno == EINVAL)) { \
            errno = 0;                                         \
            return 0;                                          \
        }                                                       \
        return rc;                                              \
    }

RSI_WRAP_FAIL_OK(chown, (const char *path, uid_t owner, gid_t group), (path, owner, group))
RSI_WRAP_FAIL_OK(lchown, (const char *path, uid_t owner, gid_t group), (path, owner, group))
RSI_WRAP_FAIL_OK(fchown, (int fd, uid_t owner, gid_t group), (fd, owner, group))
RSI_WRAP_FAIL_OK(fchownat, (int dirfd, const char *path, uid_t owner, gid_t group, int flags),
                 (dirfd, path, owner, group, flags))
RSI_WRAP_FAIL_OK(setgroups, (size_t size, const gid_t *list), (size, list))
RSI_WRAP_FAIL_OK(setgid, (gid_t gid), (gid))
RSI_WRAP_FAIL_OK(setegid, (gid_t gid), (gid))
RSI_WRAP_FAIL_OK(setregid, (gid_t rgid, gid_t egid), (rgid, egid))
RSI_WRAP_FAIL_OK(setresgid, (gid_t rgid, gid_t egid, gid_t sgid), (rgid, egid, sgid))
RSI_WRAP_FAIL_OK(setuid, (uid_t uid), (uid))
RSI_WRAP_FAIL_OK(seteuid, (uid_t uid), (uid))
RSI_WRAP_FAIL_OK(setreuid, (uid_t ruid, uid_t euid), (ruid, euid))
RSI_WRAP_FAIL_OK(setresuid, (uid_t ruid, uid_t euid, uid_t suid), (ruid, euid, suid))
