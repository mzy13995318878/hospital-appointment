/**
 * 四个页面共用的界面零件。
 *
 * 与 api.js 的分工：
 *   api.js 只管"怎么跟后端说话"，ui.js 只管"怎么把东西画出来"。
 * 两者互不相干，所以后端改了接口形状只动 api.js，
 * 界面改了布局只动 ui.js —— 一次改动不会同时踩两边。
 *
 * 这个文件存在的直接理由是：
 * 导航栏、提示条、日期格式化在四个页面里都要用。
 * 不抽出来就得复制四份，将来改一处文案要改四个地方，
 * 而"改了三个漏了一个"是最难发现的那类 bug。
 */

(function (global) {
  'use strict';

  // ------------------------------------------------------------------
  // 安全：所有拼进 HTML 的数据都必须先过这一道
  // ------------------------------------------------------------------

  /**
   * 转义 HTML 特殊字符。
   *
   * 为什么必须有：医生的姓名来自种子数据（可控），
   * 但**候补名单里的姓名、预约记录里的姓名来自用户输入** —— 登录接口接收任意文本。
   * 若有人把姓名填成 `<img src=x onerror=alert(1)>`，
   * 列表页用 innerHTML 渲染时就会原样执行。
   *
   * 提示消息刻意不经过这里：toast 用 textContent 赋值，
   * 浏览器不解析标签，天然安全且省一次转换。
   */
  function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ------------------------------------------------------------------
  // 日期
  // ------------------------------------------------------------------

  /**
   * 把 "2026-10-26" 解析成本地时间的日期对象。
   *
   * 刻意不用 new Date("2026-10-26")：
   * 那个写法按 **UTC** 解析，得到的是本地时间 08:00。
   * 它本身没错，但一旦拿它去和"今天"比较，
   * 在 UTC+N 时区下早晨的几个小时会算出昨天。
   * 手动拆字符串构造，得到的就是干净的当地零点。
   */
  function parseDate(text) {
    if (!text) return null;
    var parts = String(text).split('-');
    if (parts.length !== 3) return null;
    var d = new Date(+parts[0], +parts[1] - 1, +parts[2]);
    return isNaN(d.getTime()) ? null : d;
  }

  var WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

  /**
   * 给日期加上"今天/明天/后天"的相对说法。
   *
   * 必要性来自真实场景：挂号时用户真正想看的不是"2026-10-26"，
   * 而是"是不是明天"。绝对日期要靠脑子换算，相对日期一眼就懂。
   */
  function formatDate(text) {
    var d = parseDate(text);
    if (!d) return text || '';

    var base = parseDate(today());
    var baseText = '';
    if (base) {
      var days = Math.round((d - base) / 86400000);
      if (days === 0) baseText = '今天';
      else if (days === 1) baseText = '明天';
      else if (days === 2) baseText = '后天';
      else if (days === -1) baseText = '昨天';
    }

    var absolute =
      d.getMonth() + 1 + '月' + d.getDate() + '日 周' + WEEKDAYS[d.getDay()];

    // 相对说法在前、绝对日期在后：
    // 扫一眼先拿到要的信息（哪天），需要确认时还有精确日期兜底。
    return baseText ? baseText + '（' + absolute + '）' : absolute;
  }

  function today() {
    var now = new Date();
    var m = String(now.getMonth() + 1);
    var d = String(now.getDate());
    // 手动补零而不是用 toISOString()：后者会先转成 UTC，
    // 凌晨时段会给出前一天的字符串。
    return (
      now.getFullYear() +
      '-' +
      (m.length < 2 ? '0' + m : m) +
      '-' +
      (d.length < 2 ? '0' + d : d)
    );
  }

  // ------------------------------------------------------------------
  // 提示条
  // ------------------------------------------------------------------

  /**
   * @param {string} message 提示文字
   * @param {string} type    success / error / info
   * @param {number} ms      停留时长。错误提示停留更久，
   *                         因为用户可能需要读完整句话才知道怎么改。
   */
  function toast(message, type, ms) {
    var wrap = document.getElementById('toast-wrap');
    if (!wrap) return;

    var el = document.createElement('div');
    el.className = 'toast toast-' + (type || 'info');
    // textContent 而不是 innerHTML：消息里可能有服务端返回的姓名等文本，
    // 交给浏览器当纯文本处理，标签不会被解析。
    el.textContent = message;
    wrap.appendChild(el);

    setTimeout(function () {
      el.style.transition = 'opacity 0.2s';
      el.style.opacity = '0';
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 200);
    }, ms || (type === 'error' ? 6000 : 4000));
  }

  // ------------------------------------------------------------------
  // 排班卡片
  // ------------------------------------------------------------------

  /**
   * 渲染一张排班卡片。
   *
   * 列表页与医生详情页共用这一个函数，不是各写一份。
   * 两页的差别只有一处：详情页的医生信息已经在页头了，卡片里再写一遍是重复。
   *
   * 共用的收益不只是少写代码 —— 它保证了**同一条排班在两个页面长得一样**。
   * 各写一份时，改了列表页忘了详情页，用户就会看到两个页面对同一件事说法不一致。
   * 这与后端"状态只有一处计算"是同一个道理，只是搬到了前端。
   *
   * @param {object} s 后端给的排班对象
   * @param {object} [options]
   * @param {boolean} [options.showDoctor] 是否显示医生名与科室（详情页传 false）
   * @param {object} [options.mine] schedule_id → {id, seq_no}，
   *        用于在卡片上直接显示"你已持有几号"并给出退号按钮
   */
  function scheduleCard(s, options) {
    options = options || {};

    // 日期已过：后端仍会给出原本的状态（比如 open），
    // 但点击后会被 SCHEDULE_EXPIRED 拦下。
    // 前端提前置灰只是友好提醒，**真正的拦截永远在后端** ——
    // 与防超卖同一套思路：前端防君子，数据库防小人。
    var isPast = s.work_date < today();

    // 这个人在这个排班上已经持有的号
    var mine = (options.mine || {})[s.schedule_id];

    var doctorPart = '';
    if (options.showDoctor !== false) {
      doctorPart =
        '<span class="doctor-name">' +
          '<a href="doctor.html?id=' + s.doctor_id + '">' +
            escapeHtml(s.doctor_name) +
          '</a>' +
        '</span>' +
        '<span class="doctor-title">' + escapeHtml(s.title) + '</span>' +
        '<span class="dept-tag">' + escapeHtml(s.department) + '</span>';
    }

    var head =
      '<div class="card-head">' +
        '<div>' + doctorPart + '</div>' +
        '<span class="status status-' + s.status + '">' +
          escapeHtml(s.status_label) +
        '</span>' +
      '</div>';

    var waitingPart = '';
    if (s.waiting_count > 0) {
      waitingPart =
        '<span>候补 <span class="slot-num">' + s.waiting_count + '</span> 人</span>';
    }

    var line =
      '<div class="slot-line">' +
        '<span>' + formatDate(s.work_date) + ' ' + escapeHtml(s.period_label) + '</span>' +
        '<span>号源 <span class="slot-num">' + s.booked_count + '</span> / ' + s.total_slots + '</span>' +
        '<span>剩余 <span class="slot-remaining' + (s.remaining === 0 ? ' zero' : '') + '">' +
          s.remaining + '</span></span>' +
        waitingPart +
      '</div>';

    var foot =
      '<div class="card-foot">' +
        '<span class="card-note">' + cardNote(s, isPast, mine) + '</span>' +
        cardAction(s, isPast, mine) +
      '</div>';

    return '<div class="card card-' + s.status + '">' + head + line + foot + '</div>';
  }

  function cardNote(s, isPast, mine) {
    if (isPast) return '该日期已过';
    if (mine) return '你已持有 ' + mine.seq_no + ' 号';
    if (s.status === 'waiting') {
      return '已有 ' + s.waiting_count + ' 人在排队，加入后你会排在最后';
    }
    if (s.status === 'full') return '号已放完，暂不接受候补';
    if (s.status === 'closed') return '医生该时段停诊';
    return '';
  }

  /**
   * 卡片右下角的按钮。
   *
   * 四种状态各给一个按钮，而不是一律给"预约"再让后端拒绝。
   * 一律给"预约"的话，用户非得点一下才知道不行，拿到的是一句冷冰冰的错误提示；
   * 按状态给按钮，等于把"报错"提前成"预告"。
   */
  function cardAction(s, isPast, mine) {
    if (isPast) return '<button class="btn btn-default" disabled>日期已过</button>';

    // 已经持有号的人，这个卡片对他的意义是"退号"而不是"再约一个"
    if (mine) {
      return (
        '<button class="btn btn-danger" data-cancel="' + mine.id + '">退号</button>'
      );
    }
    if (s.status === 'closed') return '<button class="btn btn-default" disabled>已停诊</button>';
    if (s.status === 'full') return '<button class="btn btn-default" disabled>号源已满</button>';

    if (s.status === 'waiting') {
      return (
        '<button class="btn btn-outline" data-book="' + s.schedule_id + '">' +
        '加入候补（第 ' + (s.waiting_count + 1) + ' 位）' +
        '</button>'
      );
    }
    return '<button class="btn btn-primary" data-book="' + s.schedule_id + '">预约</button>';
  }

  // ------------------------------------------------------------------
  // 导航栏
  // ------------------------------------------------------------------

  /**
   * 渲染导航栏的两件动态部分：当前页高亮、右侧身份区。
   *
   * 导航的 HTML 骨架由各页面自己写（结构固定），
   * 这里只填变化的部分 —— 骨架写死是为了让页面结构一眼可读，
   * 不必去 JS 里找导航栏长什么样。
   */
  function renderNav() {
    var page = API.currentPage();

    var links = document.querySelectorAll('[data-nav]');
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute('data-nav') === page) {
        links[i].className = 'active';
      }
    }

    var slot = document.getElementById('nav-user');
    if (!slot) return;

    var user = API.currentUser();
    if (user && user.user_id) {
      // 登录态：显示姓名 + 退出
      slot.innerHTML =
        '<strong>' + escapeHtml(user.name) + '</strong>' +
        '<span class="sep">|</span>' +
        '<a href="#" id="nav-logout">退出</a>';

      document.getElementById('nav-logout').addEventListener(
        'click',
        function (event) {
          event.preventDefault();
          API.clearUser();
          // 退出后回列表页。
          // 不留在原页：我的预约页在清空身份后是空的，
          // 停在那里只会让用户以为系统出了故障。
          location.href = 'index.html';
        }
      );
    } else {
      // 未登录：把当前页记进 from，登录完能回来
      slot.innerHTML =
        '<a href="login.html?from=' +
        encodeURIComponent(page + location.search) +
        '">登录</a>';
    }
  }

  /**
   * 把错误码翻译成人能看懂的提示。
   *
   * 后端给的是 code（如 DAILY_DEPT_LIMIT）与一句简短 message。
   * 直接显示 message 能用，但它通常只说"你今天在这个科室已经有号了"，
   * 没说接下来该怎么办。这里补上那半句。
   */
  function explain(err) {
    if (!(err instanceof API.ApiError)) {
      return err && err.message ? err.message : '操作失败';
    }

    var tips = {
      DAILY_DEPT_LIMIT:
        '同一天在同一科室只能挂一个号，请换个科室或换个日期',
      ALREADY_BOOKED_THIS_SLOT: '你在这个时段已经有号了',
      ALREADY_WAITING: '你已经在这个时段的候补队列里了',
      SCHEDULE_CLOSED: '该时段已停诊，请选择其它时段',
      SCHEDULE_EXPIRED: '该排班日期已过，不能预约',
      SCHEDULE_NOT_FOUND: '该排班不存在，可能已被取消',
      USER_BLACKLISTED:
        '你的账号因多次爽约被暂停预约，请到医院窗口处理',
      UNAUTHORIZED: '登录已失效，请重新登录',
      NETWORK: '无法连接服务器，请确认服务已启动',
    };

    var extra = tips[err.code];
    if (!extra) return err.message;

    // 后端 message 与前端补充说明都保留：
    // message 说明的是"出了什么事"，extra 说明的是"你该怎么办"。
    return err.message + ' —— ' + extra;
  }

  global.UI = {
    escapeHtml: escapeHtml,
    parseDate: parseDate,
    formatDate: formatDate,
    today: today,
    toast: toast,
    renderNav: renderNav,
    explain: explain,
    scheduleCard: scheduleCard,
  };
})(window);
