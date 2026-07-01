// Wiki Core API client for the Ethara Employee Portal Flutter app.
//
// Covers every wiki_core endpoint: app-facing reads, employee writes
// (grievance + leave), and admin CRUD over all content models.
//
// Depends on `dio`. Add to pubspec.yaml:
//   dependencies:
//     dio: ^5.4.0
//
// Usage:
//   final api = WikiApiClient(baseUrl: 'https://etp.ethara.com');
//   await api.login('user@ethara.com', 'secret');   // stores the token
//   final dash = await api.dashboard();
//   final faqs = await api.faqs();
//   await api.createFaq({'name': 'Q?', 'answer': 'A', 'group': 'General'});

import 'package:dio/dio.dart';

/// Thrown when the API returns a non-200 envelope (status_code != 200).
class WikiApiException implements Exception {
  final int statusCode;
  final String message;
  final List errors;
  WikiApiException(this.statusCode, this.message, this.errors);
  @override
  String toString() => 'WikiApiException($statusCode): $message';
}

/// The valid admin CRUD content resources (mirrors RESOURCES in the backend).
enum WikiResource {
  categories,
  updates,
  faqs,
  holidays,
  trainingGroups,
  trainingDocs,
  articles,
  articleSections,
  processFlows,
  processStages,
}

extension on WikiResource {
  String get path {
    switch (this) {
      case WikiResource.trainingGroups:
        return 'training_groups';
      case WikiResource.trainingDocs:
        return 'training_docs';
      case WikiResource.articleSections:
        return 'article_sections';
      case WikiResource.processFlows:
        return 'process_flows';
      case WikiResource.processStages:
        return 'process_stages';
      default:
        return name; // categories, updates, faqs, holidays, articles
    }
  }
}

class WikiApiClient {
  WikiApiClient({required String baseUrl, Dio? dio})
      : _dio = dio ?? Dio(BaseOptions(baseUrl: baseUrl));

  final Dio _dio;
  String? _accessToken;
  String? get accessToken => _accessToken;

  /// Inject a previously stored token (e.g. from secure storage on launch).
  void setToken(String token) => _accessToken = token;

  Options get _authOptions =>
      Options(headers: {if (_accessToken != null) 'access_token': _accessToken});

  /// Unwraps the standard envelope and surfaces backend errors as exceptions.
  Map<String, dynamic> _unwrap(Response res) {
    final body = (res.data is Map) ? res.data as Map<String, dynamic> : {};
    final status = body['status_code'] ?? res.statusCode ?? 0;
    if (status != 200) {
      throw WikiApiException(
        status is int ? status : int.tryParse('$status') ?? 0,
        (body['message'] ?? 'Request failed').toString(),
        (body['errors'] as List?) ?? const [],
      );
    }
    return (body['data'] as Map<String, dynamic>?) ?? <String, dynamic>{};
  }

  // ── Auth ───────────────────────────────────────────────────────────
  Future<Map<String, dynamic>> login(String login, String password) async {
    final res = await _dio.post('/api/v1/auth_token',
        data: {'login': login, 'password': password});
    final data = _unwrap(res);
    _accessToken = data['access_token'] as String?;
    return data;
  }

  // ── App-facing reads ───────────────────────────────────────────────
  Future<Map<String, dynamic>> dashboard() => _get('/api/v1/wiki/dashboard');
  Future<Map<String, dynamic>> faqs() => _get('/api/v1/wiki/faqs');
  Future<Map<String, dynamic>> holidays({int? year}) =>
      _get('/api/v1/wiki/holidays', query: {if (year != null) 'year': year});
  Future<Map<String, dynamic>> training() => _get('/api/v1/wiki/training');
  Future<Map<String, dynamic>> articles() => _get('/api/v1/wiki/articles');
  Future<Map<String, dynamic>> processFlows() =>
      _get('/api/v1/wiki/process_flows');
  Future<Map<String, dynamic>> orgChart() => _get('/api/v1/wiki/org_chart');
  Future<Map<String, dynamic>> leaveSummary() =>
      _get('/api/v1/wiki/leave/summary');
  Future<Map<String, dynamic>> grievances() => _get('/api/v1/wiki/grievances');

  // ── Employee writes: grievances ────────────────────────────────────
  Future<Map<String, dynamic>> createGrievance({
    required String category,
    required String description,
    bool isAnonymous = false,
    bool submit = true,
  }) =>
      _post('/api/v1/wiki/grievances', {
        'category': category,
        'description': description,
        'is_anonymous': isAnonymous,
        'state': submit ? 'submitted' : 'draft',
      });

  Future<Map<String, dynamic>> readGrievance(int id) =>
      _get('/api/v1/wiki/grievances/$id');

  Future<Map<String, dynamic>> updateGrievance(
          int id, Map<String, dynamic> changes) =>
      _put('/api/v1/wiki/grievances/$id', changes);

  Future<Map<String, dynamic>> deleteGrievance(int id) =>
      _delete('/api/v1/wiki/grievances/$id');

  // ── Employee writes: leave ─────────────────────────────────────────
  Future<Map<String, dynamic>> applyLeave({
    required int leaveTypeId,
    required String dateFrom, // YYYY-MM-DD
    String? dateTo,
    String? reason,
  }) =>
      _post('/api/v1/wiki/leave/apply', {
        'leave_type_id': leaveTypeId,
        'date_from': dateFrom,
        if (dateTo != null) 'date_to': dateTo,
        if (reason != null) 'reason': reason,
      });

  Future<Map<String, dynamic>> cancelLeave(int id) =>
      _post('/api/v1/wiki/leave/$id/cancel', const {});

  // ── Admin CRUD (content) ───────────────────────────────────────────
  Future<List<dynamic>> list(WikiResource r, {String active = 'true'}) async {
    final data = await _get('/api/v1/wiki/admin/${r.path}',
        query: {'active': active});
    return (data['items'] as List?) ?? const [];
  }

  Future<Map<String, dynamic>> read(WikiResource r, int id) async {
    final data = await _get('/api/v1/wiki/admin/${r.path}/$id');
    return (data['record'] as Map<String, dynamic>?) ?? {};
  }

  Future<Map<String, dynamic>> create(
      WikiResource r, Map<String, dynamic> vals) async {
    final data = await _post('/api/v1/wiki/admin/${r.path}', vals);
    return (data['record'] as Map<String, dynamic>?) ?? {};
  }

  Future<Map<String, dynamic>> update(
      WikiResource r, int id, Map<String, dynamic> changes) async {
    final data = await _put('/api/v1/wiki/admin/${r.path}/$id', changes);
    return (data['record'] as Map<String, dynamic>?) ?? {};
  }

  Future<void> remove(WikiResource r, int id) =>
      _delete('/api/v1/wiki/admin/${r.path}/$id');

  // Convenience wrappers for the most common content type.
  Future<Map<String, dynamic>> createFaq(Map<String, dynamic> v) =>
      create(WikiResource.faqs, v);
  Future<Map<String, dynamic>> updateFaq(int id, Map<String, dynamic> v) =>
      update(WikiResource.faqs, id, v);
  Future<void> deleteFaq(int id) => remove(WikiResource.faqs, id);

  // ── Transport helpers ──────────────────────────────────────────────
  Future<Map<String, dynamic>> _get(String path,
          {Map<String, dynamic>? query}) async =>
      _unwrap(await _dio.get(path,
          queryParameters: query, options: _authOptions));

  Future<Map<String, dynamic>> _post(
          String path, Map<String, dynamic> body) async =>
      _unwrap(await _dio.post(path, data: body, options: _authOptions));

  Future<Map<String, dynamic>> _put(
          String path, Map<String, dynamic> body) async =>
      _unwrap(await _dio.put(path, data: body, options: _authOptions));

  Future<Map<String, dynamic>> _delete(String path) async =>
      _unwrap(await _dio.delete(path, options: _authOptions));
}
