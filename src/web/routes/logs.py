"""Logs HTTP routes registered without endpoint namespaces."""

from typing import Any

_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Logs endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ("/logs", "logs_view", logs_view),
        ("/api/logs", "api_logs", api_logs),
        ("/api/logs/live", "api_logs_live", api_logs_live),
        ("/view_log", "view_log", view_log),
    )
    for rule, endpoint, view in rules:
        app.add_url_rule(rule, endpoint, view)


def logs_view():
    """Display logs frontend with filtering capabilities.

    `?embed=1` renders without the page chrome so the viewer can be hosted
    inside an iframe (the Settings → Logs tab)."""
    web = _web_server()
    embed = web.request.args.get('embed') in ('1', 'true', 'yes')
    return web.render_template(web.ui_template('logs.html'), embed=embed)


def api_logs():
    """API endpoint for fetching logs with filtering and pagination."""
    web = _web_server()
    try:
        # Get query parameters
        lines_count = web.request.args.get('lines', 1000, type=int)
        min_level = web.request.args.get('level', 'DEBUG')
        search_term = web.request.args.get('search', '').lower()
        offset = web.request.args.get('offset', 0, type=int)

        # Limit lines count for performance
        lines_count = min(lines_count, 5000)

        # Read log files (current and backups)
        all_lines = []

        # Read current log file
        if web.LOG_PATH and web.LOG_PATH.exists():
            all_lines.extend(web._tail_text_lines(web.LOG_PATH, lines_count))

        # Read backup files if needed (for more history)
        if web.LOG_PATH and lines_count > len(all_lines):
            for i in range(1, 6):  # Check up to 5 backup files
                backup_path = web.Path(str(web.LOG_PATH) + f'.{i}')
                if backup_path.exists():
                    remaining = lines_count - len(all_lines)
                    backup_lines = web._tail_text_lines(backup_path, remaining)
                    all_lines = backup_lines + all_lines
                    if len(all_lines) >= lines_count:
                        break

        # Parse and filter logs
        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        parsed_logs = []
        for line in all_lines:
            line = line.strip()
            if not line:
                continue

            # Parse log line format: [2024-01-09 10:30:45] LEVEL - MODULE: MESSAGE
            try:
                if line.startswith('[') and '] ' in line:
                    timestamp_end = line.find('] ')
                    timestamp_str = line[1:timestamp_end]
                    rest = line[timestamp_end + 2:]

                    if ': ' in rest:
                        level_module_str, message = rest.split(': ', 1)

                        # Check if format includes module (LEVEL - MODULE)
                        if ' - ' in level_module_str:
                            level_str, module_str = level_module_str.split(' - ', 1)
                        else:
                            # Old format without module
                            level_str = level_module_str
                            module_str = 'unknown'

                        level_num = log_levels.get(level_str.upper(), 20)

                        # Apply filters
                        if level_num >= min_level_num:
                            if not search_term or search_term in message.lower() or search_term in level_str.lower() or search_term in module_str.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': level_str,
                                    'message': message,
                                    'module': module_str,
                                    'raw': line
                                })
                    else:
                        # Line without level, treat as INFO
                        if min_level_num <= 20:
                            if not search_term or search_term in rest.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': 'INFO',
                                    'message': rest,
                                    'module': 'unknown',
                                    'raw': line
                                })
                else:
                    # Raw line without timestamp, treat as INFO
                    if min_level_num <= 20:
                        if not search_term or search_term in line.lower():
                            parsed_logs.append({
                                'timestamp': '',
                                'level': 'INFO',
                                'message': line,
                                'module': 'unknown',
                                'raw': line
                            })
            except Exception:
                # If parsing fails, include as raw line
                if not search_term or search_term in line.lower():
                    parsed_logs.append({
                        'timestamp': '',
                        'level': 'INFO',
                        'message': line,
                        'module': 'unknown',
                        'raw': line
                    })

        # Get recent logs first, then apply pagination
        recent_logs = parsed_logs[-lines_count:] if len(parsed_logs) > lines_count else parsed_logs

        # Apply offset for pagination
        if offset > 0:
            recent_logs = recent_logs[:-offset] if offset < len(recent_logs) else []

        return web.jsonify({
            'logs': recent_logs,
            'total_lines': len(parsed_logs),
            'displayed_lines': len(recent_logs),
            'has_more': len(parsed_logs) > lines_count + offset
        })

    except Exception as e:
        web.logger.error(f"❌ Error fetching logs: {e}", exc_info=True)
        return web.jsonify({'error': 'Failed to fetch logs', 'logs': [], 'total_lines': 0, 'displayed_lines': 0}), 500


def api_logs_live():
    """API endpoint for fetching recent live logs from memory."""
    web = _web_server()
    try:
        # Get query parameters
        count = web.request.args.get('count', 50, type=int)
        min_level = web.request.args.get('level', 'DEBUG')
        search_term = web.request.args.get('search', '').lower()

        # Limit count for performance
        count = min(count, 500)

        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        # Get recent logs from memory
        recent_logs = web.memory_log_handler.get_recent_logs(count * 2)  # Get more to filter

        # Filter logs
        filtered_logs = []
        for log_entry in recent_logs:
            level_num = log_levels.get(log_entry['level'], 20)

            # Apply filters
            if level_num >= min_level_num:
                if not search_term or search_term in log_entry['message'].lower() or search_term in log_entry['level'].lower():
                    filtered_logs.append(log_entry)

        # Return most recent filtered logs
        result_logs = filtered_logs[-count:] if len(filtered_logs) > count else filtered_logs

        return web.jsonify({
            'logs': result_logs,
            'timestamp': web.datetime.now().isoformat()
        })

    except Exception as e:
        web.logger.error(f"❌ Error fetching live logs: {e}", exc_info=True)
        return web.jsonify({'error': 'Failed to fetch live logs', 'logs': [], 'timestamp': web.datetime.now().isoformat()}), 500


def view_log():
    """Legacy endpoint - redirect to new logs page."""
    web = _web_server()
    return web.redirect(web.url_for('logs_view'))
