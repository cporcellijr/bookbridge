"""Diagnostics HTTP routes registered without endpoint namespaces."""

from typing import Any

from src.web.auth import admin_required, bind_web_module as _bind_auth_web_module


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Diagnostics endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    _bind_auth_web_module(web_module)
    rules = (
        ('/api/diagnostics/send-now', 'api_diagnostics_send_now', api_diagnostics_send_now, ['POST']),
        ('/api/diagnostics/opt-in', 'api_diagnostics_opt_in', api_diagnostics_opt_in, ['POST']),
        ('/my-reports', 'my_reports', my_reports, ['GET']),
        ('/api/diagnostics/submissions', 'api_diagnostics_submissions', api_diagnostics_submissions, ['GET']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



@admin_required
def api_diagnostics_send_now():
    """Submit a manual diagnostics report for this BookBridge instance."""
    web = _web_server()
    body = web.request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return web.jsonify({'error': 'Request body must be a JSON object.'}), 400
    message = body.get('message', '')
    if not isinstance(message, str):
        return web.jsonify({'error': 'Message must be a string.'}), 400
    message = message.strip()
    if len(message) > 2000:
        return web.jsonify({'error': 'Message must be 2000 characters or fewer.'}), 400

    result = web._run_diagnostics_send(
        force=True,
        manual=True,
        user_message=message,
    )
    if result.get('sent') or result.get('reason') in ('opt_out', 'no_endpoint'):
        return web.jsonify(result), 200
    if result.get('reason') == 'http_429':
        return web.jsonify(result), 429
    return web.jsonify(result), 502


@admin_required
def api_diagnostics_opt_in():
    """Admin endpoint: toggle diagnostics opt-in state."""
    web = _web_server()
    body = web.request.get_json(silent=True) or {}
    opted_in = bool(body.get('opt_in'))
    val = 'true' if opted_in else 'false'
    web.database_service.set_setting('DIAGNOSTICS_OPT_IN', val)
    web.os.environ['DIAGNOSTICS_OPT_IN'] = val
    web.database_service.set_setting('DIAGNOSTICS_PROMPTED', 'true')
    web.os.environ['DIAGNOSTICS_PROMPTED'] = 'true'

    instance_id = ''
    if opted_in:
        from src.services import diagnostics
        instance_id = diagnostics.ensure_instance_id(web.database_service)

    return web.jsonify({'ok': True, 'opt_in': opted_in, 'instance_id': instance_id})


@admin_required
def my_reports():
    """Redirect the retired technical reports page to Diagnostics settings."""
    web = _web_server()
    return web.redirect(web.url_for('settings') + '#system')


@admin_required
def api_diagnostics_submissions():
    """Return this instance's manual report history without exposing its token."""
    web = _web_server()
    base_url, token, error = web._diagnostics_receiver_context()
    if error:
        if error == 'Diagnostics ingest token is not configured.':
            return web.jsonify({'submissions': []})
        return web.jsonify({'error': error}), 503

    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = web.requests.get(
            f'{base_url}/api/v1/my/submissions', headers=headers, timeout=15,
        )
    except web.requests.RequestException:
        return web.jsonify({'error': 'Could not reach the diagnostics receiver.'}), 502

    if resp.status_code != 200:
        return web.jsonify({'error': 'The diagnostics receiver returned an error.'}), 502

    try:
        data = resp.json()
    except ValueError:
        return web.jsonify({'error': 'The diagnostics receiver returned an invalid response.'}), 502
    if not isinstance(data, dict) or not isinstance(data.get('submissions'), list):
        return web.jsonify({'error': 'The diagnostics receiver returned an invalid response.'}), 502

    submissions = []
    for item in data['submissions']:
        if not isinstance(item, dict):
            continue
        user_message = item.get('user_message')
        response_md = item.get('response_md')
        submitted_at = item.get('submitted_at') or item.get('received_at')
        submissions.append({
            'id': item.get('id'),
            'submitted_at': submitted_at if isinstance(submitted_at, str) else '',
            'user_message': user_message if isinstance(user_message, str) else '',
            'response_md': response_md if isinstance(response_md, str) else '',
            'response_at': item.get('response_at') if isinstance(item.get('response_at'), str) else '',
            'status': 'replied' if (
                item.get('status') == 'replied'
                or isinstance(response_md, str) and bool(response_md.strip())
            ) else 'received',
        })
    return web.jsonify({'submissions': submissions})
