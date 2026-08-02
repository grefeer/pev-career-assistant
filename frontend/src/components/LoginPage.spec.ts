import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ push: vi.fn() }));
const auth = vi.hoisted(() => ({ login: vi.fn(), register: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));
vi.mock("../state/auth", () => ({ useAuth: () => auth }));

import LoginPage from "./LoginPage.vue";

describe("login page", () => {
  beforeEach(() => vi.clearAllMocks());

  it("describes the evidence-first PEV assistant instead of a legacy graph product", () => {
    const wrapper = mount(LoginPage);
    expect(wrapper.text()).toContain("Planner、Executor、Verifier");
    expect(wrapper.text()).not.toContain("LangGraph");
    expect(wrapper.text()).not.toContain("SQLite checkpoint");
  });

  it("submits login credentials and moves to the assistant", async () => {
    auth.login.mockResolvedValue(undefined);
    const wrapper = mount(LoginPage);
    const inputs = wrapper.findAll("input");
    await inputs[0].setValue("student");
    await inputs[1].setValue("secret");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();
    expect(auth.login).toHaveBeenCalledWith("student", "secret");
    expect(router.push).toHaveBeenCalledWith("/");
    expect(wrapper.text()).toContain("登录成功");
  });

  it("shows registration controls and a safe error when authentication fails", async () => {
    auth.register.mockRejectedValue(new Error("账号已存在"));
    const wrapper = mount(LoginPage);
    await wrapper.findAll("button")[1].trigger("click");
    expect(wrapper.text()).toContain("昵称");
    const inputs = wrapper.findAll("input");
    await inputs[0].setValue("student");
    await inputs[1].setValue("Student");
    await inputs[2].setValue("secret");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("账号已存在");
  });

  it("registers successfully and falls back to a safe message for non-Error failures", async () => {
    auth.register.mockResolvedValue(undefined);
    const wrapper = mount(LoginPage);
    await wrapper.findAll("button")[1].trigger("click");
    const inputs = wrapper.findAll("input");
    await inputs[0].setValue("student");
    await inputs[1].setValue("Student");
    await inputs[2].setValue("secret");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();
    expect(auth.register).toHaveBeenCalledWith("student", "Student", "secret");
    expect(wrapper.text()).toContain("注册成功");

    auth.login.mockRejectedValue({});
    await wrapper.findAll("button")[0].trigger("click");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("认证失败");
  });
});
