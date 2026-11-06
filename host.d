// Single-process host for the Z8 template and PostgreSQL.
module webapp_host;

import std.conv : to;
import std.file : exists;
import std.stdio : readln, stdout, stderr, writeln;
import std.string : startsWith, replace;
import z8.runtime;
import z8.database.authentication;
import z8.database.bootstrap;
import z8.database.connection;
import z8.database.postgres;
import z8.database.scheduler : Z8ScheduledRun;
import z8.database.scheduler_logs : Z8FileSchedulerLogStore;
import z8.database.scheduler_service;
import z8.integration.postgres_authority;
import z8.webserver.civetweb;
import z8gen.project_registry;
import z8gen.p_org.p_zenframework.p_z8.p_template.p_SchemaGenerator;

class SchedulerObserver : IZ8SchedulerObserver {
    void onRuns(Z8ScheduledRun[] runs) { }
    void onError(wstring message) { stderr.writeln("Scheduler: ", message.to!string); }
}
class DisabledSchedulerControl : IZ8SchedulerControl, IZ8SchedulerSuspension {
    void restart() { }
    void suspend() { }
    void resume() { }
}

void main(string[] args) {
    if (args.length < 3) throw new Exception("usage: z8-template <postgres-connection> <schema> [--initialize] [--listen <address:port>] [--public-dir <path>] [--config <path>]");
    bool initialize; string publicDirectory = "build/public", configPath, listenAddress = "127.0.0.1:9080";
    for (size_t i = 3; i < args.length; ++i) {
        if (args[i] == "--initialize" && !initialize) initialize = true;
        else if (args[i] == "--public-dir" && i + 1 < args.length) publicDirectory = args[++i];
        else if (args[i] == "--listen" && i + 1 < args.length) listenAddress = args[++i];
        else if (args[i] == "--config" && !configPath.length && i + 1 < args.length) configPath = args[++i];
        else throw new Exception("invalid option");
    }
    if (!configPath.length && exists("server.properties")) configPath = "server.properties";
    if (configPath.length) z8_server_configure_from_file(configPath);
    auto db = new Z8PostgresConnection(args[1]); scope(exit) db.close();
    auto schema = args[2].to!wstring;
    auto registry = new Z8RuntimeRegistry(); z8_register_project(registry);
    auto previousRegistry = z8_runtime_set_registry(registry);
    scope(exit) z8_runtime_set_registry(previousRegistry);
    enum prefix = "org.zenframework.z8.server.base.table.system."w;
    Z8Object[] systemTables;
    wstring[] systemTableTypes;
    foreach (name; ["Users"w, "Roles"w, "UserRoles"w, "Entries"w, "UserEntries"w, "Sequences"w,
        "Tables"w, "Fields"w, "Requests"w, "RoleTableAccess"w, "RoleFieldAccess"w, "RoleRequestAccess"w, "Files"w, "Settings"w,
        "Jobs"w, "ScheduledJobs"w, "ScheduledJobLogs"w]) {
        auto typeName = prefix ~ name;
        systemTableTypes ~= typeName;
        auto table = z8_native_new(typeName); table.z8_ensure(); systemTables ~= table;
    }
    if (initialize) {
        auto user = new Z8UserData();
        user.id = z8_guid_create(2); user.login = "Admin"w;
        user.administrator = true; user.builtinAdministrator = true;
        user.privileges = new Z8Privileges();
        auto request = new Z8ApplicationRequest();
        request.session = new Z8ApplicationSession("initialize"w, user, user);
        auto previousRequest = z8_application_set_request(request);
        scope(exit) z8_application_set_request(previousRequest);
        auto previousConnection = z8_connection_set_current(new Z8PostgresDatabase(db, schema, registry, systemTableTypes));
        scope(exit) z8_connection_set_current(previousConnection);
        auto generator = z8_new!(Z8p_6F72672E7A656E6672616D65776F726B2E7A382E74656D706C6174652E536368656D6147656E657261746F72)();
        z8_schema_generator_execute(generator);
        writeln("initialized"); return;
    }
    z8_postgres_reconcile_registered(db, schema, registry, systemTables);
    Z8PostgresAuthenticationConfig config;
    auto applicationConfig = z8_application_configuration();
    config.clientHashesPasswords = applicationConfig.clientHashesPasswords;
    config.maxFailedAttempts = applicationConfig.authFailsLimit;
    config.banMilliseconds = cast(long)applicationConfig.authFailsBanSeconds * 1000;
    if (applicationConfig.uploadMaxMiB <= 0 || cast(ulong)applicationConfig.uploadMaxMiB * 1_048_576UL > size_t.max)
        throw new Exception("invalid upload limit");
    auto bodyLimit = cast(size_t)applicationConfig.uploadMaxMiB * 1_048_576;
    auto securityLog = z8_security_log_file_from_config();
    auto sessions = new Z8SessionManager(cast(long)applicationConfig.sessionTimeoutMinutes * 60_000, applicationConfig.oneSessionPerUser);
    auto authority = new Z8PostgresHttpAuthority(db, schema, registry, config, sessions, "template"w);
    auto logStore = new Z8FileSchedulerLogStore(z8_file_resolve_path(z8_file_from_path("logs/scheduler"w)));
    auto scheduler = new Z8PostgresSchedulerService(db, schema, registry, 1000, new SchedulerObserver(), authority, authority, logStore);
    if (applicationConfig.schedulerEnabled) scheduler.start();
    scope(exit) if (applicationConfig.schedulerEnabled) scheduler.stop();
    IZ8SchedulerControl schedulerControl = applicationConfig.schedulerEnabled ? cast(IZ8SchedulerControl)scheduler : new DisabledSchedulerControl();
    auto server = new Z8CivetServer(listenAddress, (Z8HttpRequest request) {
        auto connection = db.openSibling(); scope(exit) connection.close();
        auto previous = z8_connection_set_current(new Z8PostgresDatabase(connection, schema, registry, systemTableTypes));
        scope(exit) z8_connection_set_current(previous);
        z8_application_request().schedulerControl = schedulerControl;
        if (request.path.startsWith("/storage/")) z8_application_dispatch_file_download(request.path.to!wstring);
        else z8_application_dispatch_request();
    }, bodyLimit, Z8ResponseMode.application, "template"w, authority, "Access denied"w, publicDirectory, "/request.json", true, securityLog);
    scope(exit) server.stop();
    writeln(server.port()); stdout.flush(); readln();
}
