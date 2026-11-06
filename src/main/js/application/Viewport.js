// JS-класс, описывающий Viewport приложения
// Viewport - это окно приложения, в котором открываются формы
Z8.define('org.zenframework.z8.template.Viewport', {
	extend: 'Z8.application.viewport.Viewport',

	// Метод возвращает массив кнопок в правом верхнем углу окна
	createHeaderButtons: function() {
		var buttons = this.callParent();
		var login = User.login;
		var user = this.userButton = new Z8.button.Button({ text: login, tooltip: login, cls: 'btn-tool', icon: 'fa-user', enabled: false });
		return buttons.insert(user, 0);
	}
});