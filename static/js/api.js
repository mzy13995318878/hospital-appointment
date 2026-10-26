/**
 * 全站唯一的接口出口。
 *
 * 四个页面的所有请求都必须经过这里，不许各自去写 fetch。
 * 理由不是为了"整洁"，而是为了一个具体的好处：
 * **错误只有一种解析方式**。后端已经在 errors.py 里保证对外只有一种错误形状
 * （连框架的 422 都收编成了 400 INVALID_PARAM），
 * 那么前端就只需要在一个地方写「取 code、取 message」，
 * 而不必在每个调用点各写一遍判断。
 *
 * 没有这一层时，页面代码会长成这样：
 *     if (r.ok) {...} else if (r.error) {...} else if (r.detail) {...}
 * 第三种分支是给 422 准备的 —— 后端修好之后它就成了死代码，
 * 但没人敢删，因为删了没人知道会不会漏掉某个接口。
 * 收敛到一处之后，这种分支从一开始就不会被写出来。
 */

(function (global) {
  'use strict';

  // 同源部署，刻意不写域名。
  // 写死 http://127.0.0.1:8000 的话，换台机器、换个端口就全站 404；
  // 相对路径在任何地址下都成立。
  var BASE = '';

  // 会话在 localStorage 里的键名
  var USER_KEY = 'clinic.user';

  /**
   * 业务错误。
   *
   * 刻意把 code 单独存一份：页面要根据 code 做不同处理
   * （比如 DAILY_DEPT_LIMIT 提示"你今天在这个科室已经有号了"，
   * 而 SCHEDULE_CLOSED 只是把按钮置灰），只看 message 是分不出来的。
   */
  function ApiError(code, message, httpStatus) {
    this.name = 'ApiError';
    this.code = code || 'UNKNOWN';
    this.message = message || '请求失败';
    this.httpStatus = httpStatus || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  // ------------------------------------------------------------------
  // 会话
  //
  // 存 localStorage 而不是 cookie：本项目不做真实鉴权，
  // 用 cookie 还得处理同源策略与过期时间，是纯粹的额外负担。
  // ------------------------------------------------------------------

  function currentUser() {
    var raw = null;
    try {
      raw = localStorage.getItem(USER_KEY);
    } catch (e) {
      // 隐私模式下 localStorage 可能直接抛异常。
      // 这里吞掉它，让页面退化成"未登录"而不是整页崩溃。
      return null;
    }
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (e) {
      // 数据被手工改坏了 —— 当作没登录，顺便清掉，
      // 否则每次进页面都要解析一次坏 JSON。
      clearUser();
      return null;
    }
  }

  function saveUser(user) {
    try {
      localStorage.setItem(USER_KEY, JSON.stringify(user));
    } catch (e) {
      /* 存不进去就算了，本次会话内仍能用内存里的值 */
    }
    // 同时挂在 API 上：同一页面内不用反复解析 localStorage
    API.user = user;
    return user;
  }

  function clearUser() {
    try {
      localStorage.removeItem(USER_KEY);
    } catch (e) {
      /* 同上 */
    }
    API.user = null;
  }

  // ------------------------------------------------------------------
  // 核心请求
  // ------------------------------------------------------------------

  /**
   * 发一个请求并返回 data 部分。
   *
   * 失败时**一定抛 ApiError**，绝不返回 null 让调用方自己判断。
   * 理由是：返回 null 的写法会把"请求失败"和"请求成功但没有数据"
   * 混成同一种情况，而后者在本项目里几乎不存在
   * （查不到就是空数组，不是 null）。
   */
  function request(method, path, options) {
    options = options || {};

    var url = BASE + path;

    // query 参数：跳过空值。
    // 不加这层过滤，"科室=全部"会变成 ?department= 传给后端，
    // 后端拿到空字符串去匹配，结果什么都查不到 ——
    // 一个本该由前端消化的空值，变成了后端的一堆空列表。
    if (options.params) {
      var qs = new URLSearchParams();
      Object.keys(options.params).forEach(function (key) {
        var value = options.params[key];
        if (value !== null && value !== undefined && value !== '') {
          qs.append(key, value);
        }
      });
      var query = qs.toString();
      if (query) url += '?' + query;
    }

    var headers = {};
    var user = currentUser();
    if (user && user.user_id) {
      // 一律转成字符串：后端用 str 接收再手动转 int，
      // 如果这里直接传数字，HTTP 头会正常，但语义上我们依赖的是后端的转换逻辑。
      headers['X-User-Id'] = String(user.user_id);
    }
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }

    var init = { method: method, headers: headers };
    if (options.body !== undefined) {
      init.body = JSON.stringify(options.body);
    }

    // fetch 只在网络层失败时 reject，HTTP 404/500 它照样 resolve。
    // 所以下面必须自己看 resp.ok —— 这是新手最容易踩的一点。
    return fetch(url, init)
      .then(function (resp) {
        return resp
          .json()
          .catch(function () {
            return null;
          })
          .then(function (payload) {
            // 业务错误：后端给的一定是 {ok:false, error:{code, message}}
            if (payload && payload.ok === false) {
              var info = payload.error || {};
              var err = new ApiError(
                info.code,
                info.message,
                resp.status
              );
              // 401 说明本地存的身份已经失效（比如库被重建过）。
              // 不清理的话，页面会一直带着一个坏 id 去请求，每个操作都 401。
              if (resp.status === 401) {
                clearUser();
              }
              throw err;
            }
            // 后端没来得及收编的错误（比如后面的反向代理返回的 502 HTML）
            if (!resp.ok) {
              throw new ApiError(
                'HTTP_' + resp.status,
                '服务暂时不可用（HTTP ' + resp.status + '）',
                resp.status
              );
            }
            return payload ? payload.data : null;
          });
      })
      .catch(function (err) {
        // fetch 本身 reject = 连不上服务。
        // 单独给一个 code，因为提示语完全不同：
        // 业务规则的错告诉用户"你不能这么做"，网络错告诉用户"服务没起来"。
        if (err instanceof ApiError) throw err;
        throw new ApiError(
          'NETWORK',
          '无法连接服务器，请确认服务已启动',
          0
        );
      });
  }

  function get(path, params) {
    return request('GET', path, { params: params });
  }

  function post(path, body) {
    return request('POST', path, { body: body });
  }

  function del(path) {
    return request('DELETE', path);
  }

  // ------------------------------------------------------------------
  // 登录守卫
  // ------------------------------------------------------------------

  /**
   * 没有身份就跳到登录页，并记住是从哪儿来的。
   *
   * 带 from 参数是为了登录完能回到原页面。
   * 少了它，用户在医生详情页点预约 → 被踢去登录 → 登录后回到首页，
   * 他得自己再找一遍刚才那个医生。
   *
   * @returns {boolean} 是否已登录
   */
  function requireLogin() {
    if (currentUser()) return true;
    var from = location.pathname.split('/').pop() + location.search;
    location.href = 'login.html?from=' + encodeURIComponent(from || 'index.html');
    return false;
  }

  /**
   * 取当前页面名，供导航高亮用。
   */
  function currentPage() {
    var name = location.pathname.split('/').pop();
    return name || 'index.html';
  }

  // ------------------------------------------------------------------
  // 对外接口
  //
  // 刻意提供语义化方法而不是让页面直接拼 URL 字符串。
  // 拼字符串的写法下，改一个接口路径要全局搜索四遍；
  // 现在改这里一处就够了。
  // ------------------------------------------------------------------

  var API = {
    // 会话
    user: null,
    currentUser: currentUser,
    saveUser: saveUser,
    clearUser: clearUser,
    requireLogin: requireLogin,
    currentPage: currentPage,
    ApiError: ApiError,

    // 底层
    request: request,
    get: get,
    post: post,
    del: del,

    // 4.1 身份
    login: function (name, idCardLast4, phone) {
      return post('/api/auth/login', {
        name: name,
        id_card_last4: idCardLast4,
        phone: phone,
      });
    },
    me: function () {
      return get('/api/auth/me');
    },

    // 4.2 浏览
    departments: function () {
      return get('/api/departments');
    },
    schedules: function (params) {
      return get('/api/schedules', params);
    },
    doctor: function (doctorId) {
      return get('/api/doctors/' + doctorId);
    },

    // 4.3 预约
    book: function (scheduleId) {
      return post('/api/appointments', { schedule_id: scheduleId });
    },
    cancelAppointment: function (appointmentId) {
      return del('/api/appointments/' + appointmentId);
    },
    leaveWaitlist: function (waitlistId) {
      return del('/api/waitlist/' + waitlistId);
    },
    mine: function () {
      return get('/api/my/appointments');
    },

    // 5 加分项
    // 确认与签到都是 POST，且都不需要请求体 —— 用户身份走 X-User-Id，
    // 要确认/签到的目标写在路径里。
    confirmAppointment: function (appointmentId) {
      return post('/api/appointments/' + appointmentId + '/confirm', {});
    },
    checkIn: function (appointmentId) {
      return post('/api/appointments/' + appointmentId + '/check-in', {});
    },
    settle: function () {
      return post('/api/admin/settle', {});
    },
    blacklist: function (userId) {
      return post('/api/admin/users/' + userId + '/blacklist', {});
    },
    unblacklist: function (userId) {
      return del('/api/admin/users/' + userId + '/blacklist');
    },
  };

  // 启动时先恢复一次内存里的身份，省得每个页面自己读
  API.user = currentUser();

  global.API = API;
})(window);
