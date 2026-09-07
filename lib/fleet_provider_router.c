#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#define FLEET_EXPORT __declspec(dllexport)
#else
#define FLEET_EXPORT __attribute__((visibility("default")))
#endif

typedef struct {
    void *ptr;
    size_t len;
} cliproxy_buffer;

typedef struct {
    uint32_t abi_version;
    void *host_ctx;
    void *call;
    void *free_buffer;
} cliproxy_host_api;

typedef int (*plugin_call_fn)(const char *, const uint8_t *, size_t, cliproxy_buffer *);
typedef void (*plugin_free_fn)(void *, size_t);
typedef void (*plugin_shutdown_fn)(void);

typedef struct {
    uint32_t abi_version;
    plugin_call_fn call;
    plugin_free_fn free_buffer;
    plugin_shutdown_fn shutdown;
} cliproxy_plugin_api;

static void respond(cliproxy_buffer *response, const char *value) {
    size_t len = strlen(value);
    response->ptr = malloc(len);
    if (response->ptr == NULL) {
        response->len = 0;
        return;
    }
    memcpy(response->ptr, value, len);
    response->len = len;
}

static int contains(const uint8_t *value, size_t value_len, const char *wanted) {
    size_t wanted_len = strlen(wanted);
    if (wanted_len > value_len) {
        return 0;
    }
    for (size_t index = 0; index <= value_len - wanted_len; index++) {
        if (memcmp(value + index, wanted, wanted_len) == 0) {
            return 1;
        }
    }
    return 0;
}

static int plugin_call(const char *method, const uint8_t *request, size_t request_len, cliproxy_buffer *response) {
    response->ptr = NULL;
    response->len = 0;
    if (strcmp(method, "plugin.register") == 0 || strcmp(method, "plugin.reconfigure") == 0) {
        respond(response, "{\"ok\":true,\"result\":{\"schema_version\":1,\"metadata\":{\"Name\":\"Fleet provider router\",\"Version\":\"1.0.0\",\"Author\":\"Fleet\",\"GitHubRepository\":\"https://github.com/Bil0000/agents-fleet\",\"ConfigFields\":[]},\"capabilities\":{\"model_router\":true}}}");
        return 0;
    }
    if (strcmp(method, "model.route") == 0) {
        if (contains(request, request_len, "\"SourceFormat\":\"openai-response\"")) {
            respond(response, "{\"ok\":true,\"result\":{\"Handled\":true,\"TargetKind\":\"provider\",\"Target\":\"codex\"}}");
        } else if (contains(request, request_len, "\"SourceFormat\":\"claude\"")) {
            respond(response, "{\"ok\":true,\"result\":{\"Handled\":true,\"TargetKind\":\"provider\",\"Target\":\"claude\"}}");
        } else {
            respond(response, "{\"ok\":true,\"result\":{\"Handled\":false}}");
        }
        return 0;
    }
    respond(response, "{\"ok\":false,\"error\":{\"code\":\"unknown_method\",\"message\":\"unknown method\"}}");
    return 0;
}

static void plugin_free(void *ptr, size_t len) {
    (void)len;
    free(ptr);
}

static void plugin_shutdown(void) {}

FLEET_EXPORT int cliproxy_plugin_init(const cliproxy_host_api *host, cliproxy_plugin_api *plugin) {
    (void)host;
    if (plugin == NULL) {
        return 1;
    }
    plugin->abi_version = 1;
    plugin->call = plugin_call;
    plugin->free_buffer = plugin_free;
    plugin->shutdown = plugin_shutdown;
    return 0;
}
