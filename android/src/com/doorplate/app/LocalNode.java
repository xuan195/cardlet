package com.doorplate.app;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.SocketTimeoutException;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 名片 · 本地节点
 *
 * 每台手机自己就是一个节点：一个极小的 HTTP 服务 + 一个 UDP 发现端口。
 * 数据和语义都在 JS 那边，这里只做搬运，并且严守一条规则 ——
 * **只交换双方都已经持有的名片**：
 *   收到的推送里，target 不在我自己的名片集合内，一律丢弃；
 *   回给对方的，也只挑他问到的、且我也有 的那几张。
 */
public class LocalNode {

    private static final String TAG = "Doorplate";
    private static final int UDP_PORT = 47474;
    private static final int TCP_BASE = 47475;
    private static final String PROBE = "DOORPLATE1?";
    private static final String REPLY = "DOORPLATE1!";
    private static final long PEER_TTL = 90_000L;

    private final WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());

    /** JS 交过来的镜像：我有哪些名片、这些名片的事件 */
    private volatile String myIds = "[]";
    private volatile String myEvents = "[]";
    /** 别人推给我、还没交给 JS 的事件 */
    private final List<JSONObject> inbox = Collections.synchronizedList(new ArrayList<JSONObject>());
    /** ip:port -> Peer */
    private final Map<String, Peer> peers = new ConcurrentHashMap<>();

    private volatile int tcpPort = TCP_BASE;
    private volatile boolean running = true;
    private String myIp = "";

    private static class Peer {
        String ip;
        int port;
        long seen;
    }

    public LocalNode(WebView web) {
        this.web = web;
    }

    public void start() {
        myIp = findWifiIp();
        Log.i(TAG, "本地节点启动 ip=" + myIp);
        Thread t1 = new Thread(new Runnable() {
            @Override public void run() { serveLoop(); }
        }, "dp-http");
        t1.setDaemon(true);
        t1.start();
        Thread t2 = new Thread(new Runnable() {
            @Override public void run() { udpLoop(); }
        }, "dp-udp");
        t2.setDaemon(true);
        t2.start();
    }

    public void stop() {
        running = false;
    }

    // ------------------------------------------------------------------ JS 桥

    /** JS 把「我有谁 + 这些名片的事件」交过来，供同网手机来取 */
    @JavascriptInterface
    public void publish(String payloadJson) {
        try {
            JSONObject o = new JSONObject(payloadJson);
            JSONArray ids = o.optJSONArray("ids");
            JSONArray evs = o.optJSONArray("events");
            myIds = (ids == null ? new JSONArray() : ids).toString();
            myEvents = (evs == null ? new JSONArray() : evs).toString();
        } catch (Exception e) {
            Log.w(TAG, "publish 解析失败: " + e);
        }
    }

    /** 发现 + 交换，结果通过 window.__lanResult(...) 回调给 JS */
    @JavascriptInterface
    public void syncAsync() {
        Thread t = new Thread(new Runnable() {
            @Override public void run() { doSync(); }
        }, "dp-sync");
        t.setDaemon(true);
        t.start();
    }

    @JavascriptInterface
    public String info() {
        try {
            JSONObject o = new JSONObject();
            o.put("ip", myIp);
            o.put("port", tcpPort);
            o.put("peers", livePeers().size());
            return o.toString();
        } catch (Exception e) {
            return "{}";
        }
    }

    // ------------------------------------------------------------------ 同步

    private void doSync() {
        JSONArray collected = new JSONArray();
        int n = 0;
        try {
            scan();
            String body = "{\"ids\":" + myIds + ",\"events\":" + myEvents + "}";
            for (Peer p : livePeers()) {
                if (n >= 12) break;
                n++;
                JSONArray got = postEvents(p, body);
                if (got != null) {
                    for (int i = 0; i < got.length(); i++) {
                        JSONObject ev = got.optJSONObject(i);
                        if (ev != null) collected.put(ev);
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "同步出错: " + e);
        }

        synchronized (inbox) {
            for (JSONObject ev : inbox) collected.put(ev);
            inbox.clear();
        }

        final String js;
        try {
            JSONObject res = new JSONObject();
            res.put("peers", n);
            res.put("events", collected);
            js = "window.__lanResult && window.__lanResult(" + JSONObject.quote(res.toString()) + ")";
        } catch (Exception e) {
            return;
        }
        ui.post(new Runnable() {
            @Override public void run() {
                try { web.evaluateJavascript(js, null); } catch (Exception e) { }
            }
        });
    }

    private JSONArray postEvents(Peer p, String body) {
        HttpURLConnection c = null;
        try {
            URL url = new URL("http://" + p.ip + ":" + p.port + "/api/p2p/exchange");
            c = (HttpURLConnection) url.openConnection();
            c.setConnectTimeout(2000);
            c.setReadTimeout(4000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "text/plain; charset=utf-8");
            OutputStream os = c.getOutputStream();
            os.write(body.getBytes("UTF-8"));
            os.flush();
            os.close();
            if (c.getResponseCode() != 200) return null;
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int r;
            while ((r = in.read(buf)) != -1) bos.write(buf, 0, r);
            in.close();
            JSONObject o = new JSONObject(new String(bos.toByteArray(), "UTF-8"));
            return o.optJSONArray("events");
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ------------------------------------------------------------------ 发现

    private void scan() {
        DatagramSocket s = null;
        try {
            // 关键：把发现用的 socket 绑到 WiFi 网卡上。
            // 手机同时开着移动数据时，默认路由常常不是 WiFi，
            // 不绑的话广播会从蜂窝网卡出去，同网的另一台永远收不到。
            InetAddress bind = null;
            if (myIp.length() > 0) {
                try { bind = InetAddress.getByName(myIp); } catch (Exception e) { }
            }
            s = (bind != null) ? new DatagramSocket(new InetSocketAddress(bind, 0))
                               : new DatagramSocket();
            s.setBroadcast(true);
            byte[] msg = (PROBE + "{\"port\":" + tcpPort + "}").getBytes("UTF-8");

            // 1) 广播：最快的一条路
            for (String t : broadcastTargets()) {
                try { s.send(new DatagramPacket(msg, msg.length, InetAddress.getByName(t), UDP_PORT)); }
                catch (Exception e) { }
            }

            // 2) 逐台单播：路由器把广播丢掉时的兜底。
            //    /24 一共 254 个地址，254 个小包，开销可以忽略。
            String prefix = netPrefix();
            if (prefix != null) {
                for (int i = 1; i <= 254; i++) {
                    String ip = prefix + "." + i;
                    if (ip.equals(myIp)) continue;
                    try {
                        s.send(new DatagramPacket(msg, msg.length, InetAddress.getByName(ip), UDP_PORT));
                    } catch (Exception e) { }
                }
            }

            s.setSoTimeout(300);
            long end = System.currentTimeMillis() + 1600;
            while (System.currentTimeMillis() < end) {
                byte[] buf = new byte[512];
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(p);
                } catch (SocketTimeoutException te) {
                    continue;
                } catch (Exception e) {
                    break;
                }
                onPacket(new String(p.getData(), 0, p.getLength(), "UTF-8"),
                        p.getAddress().getHostAddress());
            }
        } catch (Exception e) {
            Log.w(TAG, "扫描失败: " + e);
        } finally {
            if (s != null) s.close();
        }
    }

    private List<String> broadcastTargets() {
        List<String> out = new ArrayList<String>();
        out.add("255.255.255.255");
        String prefix = netPrefix();
        if (prefix != null) out.add(prefix + ".255");
        return out;
    }

    /** 本机所在的 /24 前缀，比如 192.168.1 */
    private String netPrefix() {
        if (myIp == null || myIp.lastIndexOf('.') <= 0) return null;
        return myIp.substring(0, myIp.lastIndexOf('.'));
    }

    private void udpLoop() {
        try {
            // 端口复用：应用刚重启时旧 socket 可能还没释放，不复用会绑定失败，
            // 那样别的手机就永远问不到我
            DatagramSocket s = new DatagramSocket(null);
            s.setReuseAddress(true);
            s.bind(new InetSocketAddress(UDP_PORT));
            byte[] buf = new byte[512];
            while (running) {
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                s.receive(p);
                String txt = new String(p.getData(), 0, p.getLength(), "UTF-8");
                String ip = p.getAddress().getHostAddress();
                onPacket(txt, ip);
                if (txt.startsWith(PROBE)) {
                    String reply = REPLY + "{\"port\":" + tcpPort + "}";
                    byte[] rb = reply.getBytes("UTF-8");
                    s.send(new DatagramPacket(rb, rb.length, p.getAddress(), p.getPort()));
                }
            }
            s.close();
        } catch (Exception e) {
            Log.w(TAG, "UDP 监听结束: " + e);
        }
    }

    /** 别人探到我、或我收到别人的应答，都记一笔 */
    private void onPacket(String txt, String ip) {
        if (ip == null || ip.equals(myIp)) return;
        if (!txt.startsWith(PROBE) && !txt.startsWith(REPLY)) return;
        int port = TCP_BASE;
        try {
            String json = txt.substring(txt.indexOf('{'));
            port = new JSONObject(json).optInt("port", TCP_BASE);
        } catch (Exception e) { }
        Peer p = peers.get(ip + ":" + port);
        if (p == null) {
            p = new Peer();
            p.ip = ip;
            p.port = port;
            peers.put(ip + ":" + port, p);
        }
        p.seen = System.currentTimeMillis();
    }

    private List<Peer> livePeers() {
        long now = System.currentTimeMillis();
        List<Peer> out = new ArrayList<Peer>();
        for (Map.Entry<String, Peer> e : peers.entrySet()) {
            Peer p = e.getValue();
            if (now - p.seen > PEER_TTL) {
                peers.remove(e.getKey());
            } else if (!myIp.equals(p.ip)) {
                out.add(p);
            }
        }
        return out;
    }

    // ------------------------------------------------------------------ 对外的 HTTP

    private void serveLoop() {
        ServerSocket ss = null;
        for (int i = 0; i < 20 && ss == null; i++) {
            try {
                ServerSocket cand = new ServerSocket();
                cand.setReuseAddress(true);
                cand.bind(new InetSocketAddress(TCP_BASE + i));
                ss = cand;
                tcpPort = TCP_BASE + i;
            } catch (Exception e) {
                ss = null;
            }
        }
        if (ss == null) {
            Log.e(TAG, "HTTP 端口全被占了");
            return;
        }
        Log.i(TAG, "HTTP 监听 " + tcpPort);
        while (running) {
            try {
                final Socket sock = ss.accept();
                Thread t = new Thread(new Runnable() {
                    @Override public void run() { handle(sock); }
                }, "dp-req");
                t.setDaemon(true);
                t.start();
            } catch (Exception e) {
                if (!running) break;
            }
        }
    }

    private void handle(Socket sock) {
        try {
            sock.setSoTimeout(5000);
            InputStream in = sock.getInputStream();
            String head = readHead(in);
            if (head == null || head.length() == 0) return;
            String[] lines = head.split("\r\n");
            String[] first = lines[0].split(" ");
            if (first.length < 2) return;
            String method = first[0];
            String path = first[1];

            int len = 0;
            for (String l : lines) {
                int c = l.indexOf(':');
                if (c > 0 && l.substring(0, c).trim().equalsIgnoreCase("Content-Length")) {
                    try { len = Integer.parseInt(l.substring(c + 1).trim()); } catch (Exception e) { }
                }
            }
            byte[] body = new byte[Math.max(0, Math.min(len, 8 * 1024 * 1024))];
            int off = 0;
            while (off < body.length) {
                int n = in.read(body, off, body.length - off);
                if (n < 0) break;
                off += n;
            }
            String text = new String(body, 0, off, "UTF-8");

            if ("OPTIONS".equals(method)) {
                send(sock, 204, "");
            } else if ("GET".equals(method) && path.startsWith("/api/p2p/info")) {
                JSONObject o = new JSONObject();
                o.put("port", tcpPort);
                o.put("device", myIp);
                send(sock, 200, o.toString());
            } else if ("POST".equals(method) && path.startsWith("/api/p2p/exchange")) {
                send(sock, 200, exchange(text).toString());
            } else {
                send(sock, 404, "{}");
            }
        } catch (Exception e) {
            // 单个请求出错不影响节点
        } finally {
            try { sock.close(); } catch (Exception ignored) { }
        }
    }

    /** 收到对方的推送 → 只留我有的名片；同时把对方问到的、我也有的还回去 */
    private JSONObject exchange(String body) {
        JSONObject out = new JSONObject();
        try {
            JSONObject req = new JSONObject(body);
            Set<String> mine = toSet(new JSONArray(myIds));
            Set<String> asked = toSet(req.optJSONArray("ids"));
            JSONArray incoming = req.optJSONArray("events");

            int accepted = 0;
            if (incoming != null) {
                for (int i = 0; i < incoming.length(); i++) {
                    JSONObject ev = incoming.optJSONObject(i);
                    if (ev == null) continue;
                    if (!mine.contains(ev.optString("target"))) continue;   // 我没有的名片，不收
                    inbox.add(ev);
                    accepted++;
                }
            }

            JSONArray stored = new JSONArray(myEvents);
            JSONArray back = new JSONArray();
            for (int i = 0; i < stored.length(); i++) {
                JSONObject ev = stored.optJSONObject(i);
                if (ev == null) continue;
                String t = ev.optString("target");
                if (asked.contains(t) && mine.contains(t)) back.put(ev);
            }

            out.put("accepted", accepted);
            out.put("events", back);
        } catch (Exception e) {
            try { out.put("events", new JSONArray()); } catch (Exception ignored) { }
        }
        return out;
    }

    private static Set<String> toSet(JSONArray a) {
        Set<String> s = new HashSet<String>();
        if (a == null) return s;
        for (int i = 0; i < a.length(); i++) {
            String v = a.optString(i, "");
            if (v.length() > 0) s.add(v);
        }
        return s;
    }

    private static String readHead(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int a = -1, b = -1, c = -1, d = -1, x;
        while ((x = in.read()) != -1) {
            bos.write(x);
            a = b; b = c; c = d; d = x;
            if (a == 13 && b == 10 && c == 13 && d == 10) break;
            if (bos.size() > 16384) break;
        }
        return new String(bos.toByteArray(), "UTF-8");
    }

    private static void send(Socket sock, int code, String body) throws Exception {
        byte[] b = (body == null ? "" : body).getBytes("UTF-8");
        String heads = "HTTP/1.1 " + code + (code == 200 ? " OK" : code == 204 ? " No Content" : " Not Found") + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + b.length + "\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Headers: *\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Connection: close\r\n\r\n";
        OutputStream os = sock.getOutputStream();
        os.write(heads.getBytes("UTF-8"));
        if (b.length > 0) os.write(b);
        os.flush();
    }

    /**
     * 拿当前 WiFi 的 IPv4 地址。
     * 不能用「遍历网卡挑一个私网地址」—— 国内运营商的移动数据也常发 10.x，
     * 挑错了广播就会从蜂窝网卡出去，同网手机永远收不到。
     */
    private String findWifiIp() {
        try {
            Context ctx = web.getContext();
            ConnectivityManager cm =
                    (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                for (Network n : cm.getAllNetworks()) {
                    NetworkCapabilities cap = cm.getNetworkCapabilities(n);
                    if (cap == null || !cap.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue;
                    LinkProperties lp = cm.getLinkProperties(n);
                    if (lp == null) continue;
                    for (LinkAddress la : lp.getLinkAddresses()) {
                        InetAddress a = la.getAddress();
                        if (a instanceof Inet4Address && !a.isLoopbackAddress()) {
                            return a.getHostAddress();
                        }
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "取 WiFi 地址失败: " + t);
        }
        return findAnyIp();
    }

    /** 兜底：真拿不到 WiFi 网卡时，挑一个私网地址 */
    private static String findAnyIp() {
        String fallback = "";
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> addrs = ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress a = addrs.nextElement();
                    String ip = a.getHostAddress();
                    if (ip == null || ip.indexOf(':') >= 0 || ip.startsWith("127.")) continue;
                    if (ip.startsWith("192.168.")) return ip;          // 家用路由器最常见
                    if (fallback.length() == 0) fallback = ip;
                }
            }
        } catch (Exception e) { }
        return fallback;
    }
}
